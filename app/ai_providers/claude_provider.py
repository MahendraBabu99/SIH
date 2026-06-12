"""Anthropic Claude AI provider implementation.

Uses the ``anthropic`` Python SDK to communicate with the Anthropic
Messages API. Supports synchronous and streaming generation, CSV file
attachments via content blocks, and automatic token-limit retry.

Attributes:
    logger: Module-level logger for Claude provider operations.
"""

from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .base import (
    AIProvider,
    AIProviderError,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOKENS,
    _is_attachment_unsupported_error,
    _is_anthropic_streaming_required_error,
    _normalize_api_key_value,
    _resolve_timeout_seconds,
    _run_stream_with_rate_limit_retries,
    _run_with_completion_token_retry,
)
from .progress import (
    finalize_progress_stream_response,
    stream_progress_chunks,
)
from .utils import (
    _extract_anthropic_text,
    _inline_attachment_data_into_prompt,
    _split_anthropic_stream_event_text,
    stream_chunk_has_text,
)

logger = logging.getLogger(__name__)


def _iter_anthropic_stream_chunks(stream: Any) -> Iterator[Any]:
    """Yield separated answer/reasoning chunks from a Claude stream.

    Args:
        stream: Anthropic streaming response iterator.

    Yields:
        String-compatible chunks from Claude content-block deltas that
        carry answer or reasoning text.
    """
    for event in stream:
        chunk = _split_anthropic_stream_event_text(event)
        if stream_chunk_has_text(chunk):
            yield chunk


class ClaudeProvider(AIProvider):
    """Anthropic Claude provider implementation.

    Supports both synchronous and streaming generation, CSV file attachments
    via content blocks (base64-encoded PDFs or inline text), and automatic
    token-limit retry when ``max_tokens`` exceeds the model's maximum.

    Attributes:
        _api_key (str): The Anthropic API key (private to reduce
            accidental exposure in repr/debug output).
        model (str): The Claude model identifier.
        attach_csv_as_file (bool): Whether to upload CSV artifacts as
            content blocks.
        request_timeout_seconds (float): HTTP timeout in seconds.
        client: The ``anthropic.Anthropic`` SDK client instance.
    """

    _provider_display_name: str = "Claude"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_CLAUDE_MODEL,
        attach_csv_as_file: bool = True,
        request_timeout_seconds: float = DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the Claude provider.

        Args:
            api_key: Anthropic API key. Must be non-empty.
            model: Claude model identifier.
            attach_csv_as_file: If ``True``, send CSV artifacts as structured
                content blocks.
            request_timeout_seconds: HTTP timeout in seconds.

        Raises:
            AIProviderError: If the ``anthropic`` SDK is not installed or
                the API key is empty.
        """
        try:
            import anthropic
        except ImportError as error:
            raise AIProviderError(
                "anthropic SDK is not installed. Install it with `pip install anthropic`."
            ) from error

        normalized_api_key = _normalize_api_key_value(api_key)
        if not normalized_api_key:
            raise AIProviderError(
                "Claude API key is not configured. "
                "Set `ai.claude.api_key` in config/config.yaml or the ANTHROPIC_API_KEY environment variable."
            )

        self._anthropic = anthropic
        self._api_key = normalized_api_key
        self.model = model
        self.attach_csv_as_file = bool(attach_csv_as_file)
        self._csv_attachment_supported: bool | None = None
        self._attachment_lock = threading.Lock()
        self._rate_limit_error_class = anthropic.RateLimitError
        self.request_timeout_seconds = _resolve_timeout_seconds(
            request_timeout_seconds,
            DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
        )
        self.client = anthropic.Anthropic(
            api_key=normalized_api_key,
            timeout=self.request_timeout_seconds,
        )
        logger.info("Initialized Claude provider with model %s (timeout %.1fs)", model, self.request_timeout_seconds)

    def _map_api_error(self, error: Exception) -> AIProviderError:
        """Map an Anthropic SDK exception to an ``AIProviderError``.

        Overrides the base implementation to provide Claude-specific error
        messages referencing the correct config keys.

        Args:
            error: The raw SDK or network exception.

        Returns:
            An ``AIProviderError`` with a user-friendly message.
        """
        if isinstance(error, self._anthropic.APIConnectionError):
            return AIProviderError(
                "Unable to connect to Claude API. Check network access and endpoint configuration."
            )
        if isinstance(error, self._anthropic.AuthenticationError):
            return AIProviderError(
                "Claude authentication failed. Check `ai.claude.api_key` or ANTHROPIC_API_KEY."
            )
        return super()._map_api_error(error)

    def analyze_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[str]:
        """Stream generated chunks from Claude.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            max_tokens: Maximum completion tokens.

        Yields:
            String-compatible answer chunks as they are generated.

        Raises:
            AIProviderError: On empty response or API failure.
        """
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "stream": True,
        }

        def _stream_factory() -> Any:
            """Open the Claude streaming message request.

            Returns:
                The provider stream iterator.
            """
            return self._with_token_limit_retry(
                lambda kw: self.client.messages.create(**kw),
                request_kwargs,
            )

        return _run_stream_with_rate_limit_retries(
            stream_factory=_stream_factory,
            stream_text_iterator=_iter_anthropic_stream_chunks,
            rate_limit_error_type=self._anthropic.RateLimitError,
            provider_name="Claude",
            map_error=self._map_api_error,
            empty_response_message="Claude returned an empty response.",
        )

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with optional CSV file attachments via Claude content blocks.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            attachments: Optional list of attachment descriptors.
            max_tokens: Maximum completion tokens.

        Returns:
            The generated analysis text.

        Raises:
            AIProviderError: On any API or network failure.
        """
        def _request() -> str:
            """Run the Claude request with attachment fallback handling.

            Returns:
                The generated analysis text.
            """
            attachment_response = self._request_with_csv_attachments(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                attachments=attachments,
            )
            if attachment_response:
                return attachment_response

            effective_prompt = user_prompt
            if attachments:
                effective_prompt, inlined = _inline_attachment_data_into_prompt(
                    user_prompt=user_prompt,
                    attachments=attachments,
                )
                if inlined:
                    logger.info("Claude attachment fallback inlined attachment data into prompt.")

            response = self._create_message_with_stream_fallback(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": effective_prompt}],
                max_tokens=max_tokens,
            )
            text = _extract_anthropic_text(response)
            if not text:
                raise AIProviderError("Claude returned an empty response.")
            return text

        return self._run_request(_request)

    def analyze_with_progress(
        self,
        system_prompt: str,
        user_prompt: str,
        progress_callback: Callable[[dict[str, str]], None] | None,
        attachments: list[Mapping[str, str]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with streamed Claude thinking progress when available.

        Claude extended-thinking responses are emitted through streaming
        ``thinking_delta`` events, so progress mode uses a streaming Messages
        request and forwards separated thinking and answer channels to the UI.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            progress_callback: Optional callable receiving progress dicts.
            attachments: Optional list of attachment descriptors.
            max_tokens: Maximum completion tokens.

        Returns:
            The generated analysis text.

        Raises:
            AIProviderError: On empty response or API failure.
            Exception: Any exception raised by ``progress_callback`` aborts
                the stream and propagates unchanged (this is how mid-stream
                cancellation takes effect).
        """
        if progress_callback is None:
            return self.analyze_with_attachments(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                attachments=attachments,
                max_tokens=max_tokens,
            )

        messages = [
            {
                "role": "user",
                "content": self._build_progress_user_content(user_prompt, attachments),
            }
        ]
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
            "stream": True,
        }

        def _fallback_to_inlined_attachments(error: Exception) -> bool:
            """Switch a progress request to inline attachment data if needed.

            Bare HTTP 404 responses are deliberately not treated as
            attachment failures here: on the Anthropic Messages API a 404
            indicates a missing model or endpoint, so retrying the same
            request in text mode would fail identically and hide the real
            error behind a misleading attachment-fallback log entry.

            Args:
                error: The exception raised by the attachment-mode request.

            Returns:
                ``True`` if the request was rewritten with inline attachment
                data and should be retried, ``False`` if the error is not an
                attachment-unsupported failure and must propagate.
            """
            if not attachments or not _is_attachment_unsupported_error(error):
                return False

            effective_prompt, _inlined = _inline_attachment_data_into_prompt(
                user_prompt=user_prompt,
                attachments=attachments,
            )

            with self._attachment_lock:
                self._csv_attachment_supported = False
            request_kwargs["messages"] = [
                {"role": "user", "content": effective_prompt}
            ]
            logger.info(
                "Claude progress attachment blocks were unsupported; "
                "retrying with inline attachment data."
            )
            return True

        def _stream_factory() -> Any:
            """Open the Claude streaming message request."""
            try:
                return self._with_token_limit_retry(
                    lambda kw: self.client.messages.create(**kw),
                    request_kwargs,
                )
            except Exception as error:
                if not _fallback_to_inlined_attachments(error):
                    raise
                return self._with_token_limit_retry(
                    lambda kw: self.client.messages.create(**kw),
                    request_kwargs,
                )

        thinking_parts: list[str] = []
        answer_parts: list[str] = []

        def _progress_chunks(stream: Any) -> Iterator[Any]:
            """Emit progress while yielding chunks for retry tracking."""
            return stream_progress_chunks(
                chunks=_iter_anthropic_stream_chunks(stream),
                progress_callback=progress_callback,
                thinking_parts=thinking_parts,
                answer_parts=answer_parts,
            )

        stream = _run_stream_with_rate_limit_retries(
            stream_factory=_stream_factory,
            stream_text_iterator=_progress_chunks,
            rate_limit_error_type=self._anthropic.RateLimitError,
            provider_name="Claude",
            map_error=self._map_api_error,
            empty_response_message=(
                "Claude returned an empty streamed response. "
                "Try increasing max tokens."
            ),
        )
        for _chunk in stream:
            pass
        if attachments and isinstance(request_kwargs["messages"][0]["content"], list):
            with self._attachment_lock:
                self._csv_attachment_supported = True
        return finalize_progress_stream_response(
            thinking_parts,
            answer_parts,
            empty_response_message=(
                "Claude returned an empty streamed response. "
                "This can happen with reasoning-only outputs or very low token limits."
            ),
        )

    def _build_progress_user_content(
        self,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
    ) -> str | list[dict[str, Any]]:
        """Build Claude user content for streaming progress requests.

        Args:
            user_prompt: The user-facing prompt text.
            attachments: Optional list of attachment descriptors.

        Returns:
            Content blocks including the attachments when attachment mode is
            usable, otherwise the prompt string (with attachment data inlined
            when attachments were requested).

        Raises:
            AIProviderError: If a requested attachment cannot be read.
        """
        normalized_attachments = self._prepare_csv_attachments(attachments)
        if not normalized_attachments:
            effective_prompt = user_prompt
            if attachments:
                effective_prompt, inlined = _inline_attachment_data_into_prompt(
                    user_prompt=user_prompt,
                    attachments=attachments,
                )
                if inlined:
                    logger.info("Claude progress mode inlined attachment data into prompt.")
            return effective_prompt

        return self._build_attachment_content_blocks(user_prompt, normalized_attachments)

    @staticmethod
    def _build_attachment_content_blocks(
        user_prompt: str,
        normalized_attachments: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Build Claude user content blocks embedding CSV/PDF attachments.

        PDF attachments become base64 ``document`` blocks; all other
        attachments are read as text and wrapped in delimited ``text``
        blocks after the prompt text.

        Args:
            user_prompt: The user-facing prompt text for the leading block.
            normalized_attachments: Validated attachment descriptors.

        Returns:
            The list of Claude content-block dicts.

        Raises:
            AIProviderError: If an attachment file cannot be read.
        """
        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for attachment in normalized_attachments:
            attachment_path = Path(attachment["path"])
            attachment_name = attachment.get("name", attachment_path.name)
            mime_type = attachment["mime_type"].lower()
            if mime_type == "application/pdf":
                try:
                    encoded_data = base64.b64encode(attachment_path.read_bytes()).decode("ascii")
                except OSError as error:
                    raise AIProviderError(
                        f"Claude could not read requested attachment "
                        f"'{attachment_name}' at {attachment_path}: {error}"
                    ) from error
                content_blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded_data,
                        },
                    }
                )
                continue

            try:
                attachment_text = attachment_path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )
            except OSError as error:
                raise AIProviderError(
                    f"Claude could not read requested attachment "
                    f"'{attachment_name}' at {attachment_path}: {error}"
                ) from error
            content_blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"--- BEGIN ATTACHMENT: {attachment_name} ---\n"
                        f"{attachment_text.rstrip()}\n"
                        f"--- END ATTACHMENT: {attachment_name} ---"
                    ),
                }
            )
        return content_blocks

    def _request_with_csv_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None,
    ) -> str | None:
        """Attempt to send a request with CSV files as Claude content blocks.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt text.
            max_tokens: Maximum completion tokens.
            attachments: Optional list of attachment descriptors.

        Returns:
            The generated text if attachment mode succeeded, or ``None``
            if attachments were skipped or unsupported.

        Raises:
            Exception: Errors that do not indicate attachment-unsupported
                delivery propagate unchanged. This includes bare HTTP 404
                responses, which on the Anthropic Messages API mean a
                missing model or endpoint rather than an attachment
                problem, so the caller surfaces the real error instead of
                repeating the same failing request in text mode.
        """
        normalized_attachments = self._prepare_csv_attachments(attachments)
        if not normalized_attachments:
            return None

        try:
            content_blocks = self._build_attachment_content_blocks(
                user_prompt,
                normalized_attachments,
            )

            response = self._create_message_with_stream_fallback(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": content_blocks}],
                max_tokens=max_tokens,
            )
            text = _extract_anthropic_text(response)
            if not text:
                raise AIProviderError("Claude returned an empty response for file-attachment mode.")

            with self._attachment_lock:
                self._csv_attachment_supported = True
            return text
        except Exception as error:
            if _is_attachment_unsupported_error(error):
                with self._attachment_lock:
                    self._csv_attachment_supported = False
                logger.info(
                    "Claude endpoint does not support CSV attachments; "
                    "falling back to standard text mode."
                )
                return None
            raise

    def _create_message_with_stream_fallback(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        """Create a Claude message, falling back to streaming for long requests.

        Args:
            system_prompt: The system-level instruction text.
            messages: The conversation messages list.
            max_tokens: Maximum completion tokens.

        Returns:
            The Anthropic ``Message`` response object.
        """
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        try:
            return self._with_token_limit_retry(
                lambda kw: self.client.messages.create(**kw),
                request_kwargs,
            )
        except ValueError as error:
            if not _is_anthropic_streaming_required_error(error):
                raise
            logger.info(
                "Claude SDK requires streaming for long request; retrying with messages.stream()."
            )
            return self._with_token_limit_retry(
                lambda kw: self._stream_and_collect(**kw),
                request_kwargs,
            )

    def _stream_and_collect(self, **kwargs: Any) -> Any:
        """Stream a Claude request and return the final message.

        Args:
            **kwargs: Keyword arguments for ``client.messages.stream``.

        Returns:
            The final Anthropic ``Message`` response object.
        """
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def _with_token_limit_retry(
        self,
        create_fn: Callable[[dict[str, Any]], Any],
        request_kwargs: dict[str, Any],
    ) -> Any:
        """Execute a Claude API call with automatic token-limit retry.

        If the initial request is rejected because ``max_tokens`` exceeds
        the model's supported maximum, retries once with the lower limit
        extracted from the error message. Delegates to the shared
        completion-token retry helper so the retry contract stays
        identical across providers.

        Args:
            create_fn: A callable that takes the request kwargs dict and
                performs the API call.
            request_kwargs: Keyword arguments for the API call.

        Returns:
            The API response object.

        Raises:
            anthropic.BadRequestError: If the request fails for a reason
                other than token limits, or if the retry also fails.
        """
        return _run_with_completion_token_retry(
            create_fn=create_fn,
            request_kwargs=request_kwargs,
            token_parameter="max_tokens",
            bad_request_error_type=self._anthropic.BadRequestError,
            provider_name=self._provider_display_name,
        )

    def get_model_info(self) -> dict[str, str]:
        """Return Claude provider and model metadata.

        Returns:
            A dictionary with ``"provider"`` and ``"model"`` keys.
        """
        return {"provider": "claude", "model": self.model}
