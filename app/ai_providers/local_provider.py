"""OpenAI-compatible local provider implementation.

Uses the ``openai`` Python SDK pointed at a local endpoint (Ollama,
LM Studio, vLLM, or similar). Supports synchronous and streaming
generation, CSV file attachments via the Responses API when available,
automatic reasoning-block stripping for local reasoning models, and
configurable request timeouts.

Attributes:
    logger: Module-level logger for local provider operations.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterator, Mapping

from .base import (
    AIProvider,
    AIProviderError,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
    _normalize_api_key_value,
    _normalize_openai_compatible_base_url,
    _resolve_timeout_seconds,
    _run_stream_with_rate_limit_retries,
)
from .openai_compatible import OpenAICompatibleChatMixin
from .progress import (
    emit_progress_if_needed,
    finalize_progress_stream_response,
    stream_progress_chunks,
)
from .utils import _strip_leading_reasoning_blocks

logger = logging.getLogger(__name__)


class LocalProvider(OpenAICompatibleChatMixin, AIProvider):
    """OpenAI-compatible local provider implementation.

    Attributes:
        base_url (str): The normalized local endpoint base URL.
        model (str): The local model identifier.
        _api_key (str): The API key for the local endpoint (private to
            reduce accidental exposure in repr/debug output).
        attach_csv_as_file (bool): Whether to attempt file-attachment mode.
        request_timeout_seconds (float): HTTP timeout in seconds.
        client: The ``openai.OpenAI`` SDK client instance.
    """

    _provider_display_name: str = "Local/OpenAI-compatible"
    _openai_compatible_token_parameters = ("max_tokens",)
    _responses_provider_name = "Local provider"
    _responses_upload_purpose = "assistants"
    _responses_convert_csv_to_txt = False
    _stream_splits_leading_reasoning = True

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        attach_csv_as_file: bool = True,
        request_timeout_seconds: float = DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the local provider.

        Args:
            base_url: Base URL for the local endpoint. Normalized to
                include ``/v1`` if missing.
            model: Model identifier.
            api_key: API key. Defaults to ``"not-needed"``.
            attach_csv_as_file: If ``True``, attempt file uploads.
            request_timeout_seconds: HTTP timeout in seconds.

        Raises:
            AIProviderError: If the ``openai`` SDK is not installed.
        """
        try:
            import openai
        except ImportError as error:
            raise AIProviderError(
                "openai SDK is not installed. Install it with `pip install openai`."
            ) from error

        normalized_api_key = _normalize_api_key_value(api_key) or "not-needed"

        self._openai = openai
        self.base_url = _normalize_openai_compatible_base_url(
            base_url=base_url,
            default_base_url=DEFAULT_LOCAL_BASE_URL,
        )
        self.model = model
        self._api_key = normalized_api_key
        self.attach_csv_as_file = bool(attach_csv_as_file)
        self._rate_limit_error_class = openai.RateLimitError
        self.request_timeout_seconds = _resolve_timeout_seconds(
            request_timeout_seconds,
            DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
        )
        self._api_timeout_error_type = getattr(openai, "APITimeoutError", None)
        self._csv_attachment_supported: bool | None = None
        self._attachment_lock = threading.Lock()
        self.client = openai.OpenAI(
            api_key=normalized_api_key,
            base_url=self.base_url,
            timeout=self.request_timeout_seconds,
            max_retries=0,
        )
        logger.info(
            "Initialized local provider at %s with model %s (timeout %.1fs)",
            self.base_url,
            model,
            self.request_timeout_seconds,
        )

    def _map_api_error(self, error: Exception) -> AIProviderError:
        """Map an OpenAI SDK exception to an ``AIProviderError`` with local messages.

        Overrides the base implementation to add local-specific handling:
        timeout detection for connection errors, 404 detection for API
        errors, and local-specific authentication messaging.

        ``BadRequestError`` is delegated to the base mapping BEFORE the
        generic ``APIError`` branch. In the real ``openai`` SDK,
        ``BadRequestError`` subclasses ``APIError``, so without this
        delegation the generic branch would intercept context-length 400s
        and the user would see a raw API error instead of the actionable
        context-length guidance. ``NotFoundError`` (404) is not a
        ``BadRequestError`` subclass, so the missing-``/v1`` base-URL
        guidance is unaffected.

        Args:
            error: The raw SDK or network exception.

        Returns:
            An ``AIProviderError`` with a user-friendly message.
        """
        if isinstance(error, self._openai.APIConnectionError):
            return self._make_connection_error(error)
        if isinstance(error, self._openai.AuthenticationError):
            return AIProviderError(
                "Local AI endpoint rejected authentication. Check `ai.local.api_key` if your server requires one."
            )
        if isinstance(error, self._openai.BadRequestError):
            return super()._map_api_error(error)
        if isinstance(error, self._openai.APIError):
            return self._make_api_error(error)
        return super()._map_api_error(error)

    def analyze_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[str]:
        """Stream generated chunks from the local endpoint.

        Falls back to non-streaming if the endpoint reports streaming
        is unsupported.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            max_tokens: Maximum completion tokens.

        Yields:
            String-compatible chunks containing answer text and, when
            available, separate GUI-only reasoning text.

        Raises:
            AIProviderError: On empty response or API failure.
        """
        prompt_for_completion = self._build_chat_completion_prompt(user_prompt, None)
        messages = self._chat_completion_messages(system_prompt, prompt_for_completion)

        def _stream_factory() -> Any:
            """Open a local streaming completion or a non-stream fallback.

            Returns:
                The provider stream iterator, or final text when streaming is
                unsupported.

            Raises:
                BadRequestError: If the endpoint rejects the request for a
                    reason other than unsupported streaming.
            """
            try:
                return self._create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=True,
                )
            except self._openai.BadRequestError as error:
                if self._is_stream_unsupported_error(error):
                    return self._request_non_stream(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=max_tokens,
                        attachments=None,
                    )
                raise

        return self._stream_openai_compatible_chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            prompt_for_completion=prompt_for_completion,
            stream_factory=_stream_factory,
            empty_response_message=(
                "Local AI provider returned an empty streamed response. "
                "Try a different local model or increase max tokens."
            ),
        )

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with optional CSV file attachments.

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
            """Run the non-streaming local request.

            Returns:
                The generated analysis text.
            """
            return self._request_non_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                attachments=attachments,
            )

        return self._run_request(_request)

    def _make_connection_error(self, error: Exception) -> AIProviderError:
        """Map APIConnectionError to AIProviderError with timeout detection.

        Args:
            error: The connection error to map.

        Returns:
            An ``AIProviderError`` with an appropriate message.
        """
        if (
            self._api_timeout_error_type is not None
            and isinstance(error, self._api_timeout_error_type)
        ) or "timeout" in str(error).lower():
            return AIProviderError(
                "Local AI request timed out after "
                f"{self.request_timeout_seconds:g} seconds. "
                "Increase `ai.local.request_timeout_seconds` for long-running prompts."
            )
        return AIProviderError(
            "Unable to connect to local AI endpoint. Check `ai.local.base_url` and ensure the server is running."
        )

    def _make_api_error(self, error: Exception) -> AIProviderError:
        """Map APIError to AIProviderError with 404 detection.

        Args:
            error: The API error to map.

        Returns:
            An ``AIProviderError`` with an appropriate message.
        """
        error_text = str(error).lower()
        if "404" in error_text or "not found" in error_text:
            return AIProviderError(
                "Local AI endpoint returned 404 (not found). "
                "This is often caused by a base URL missing `/v1`. "
                f"Current base URL: {self.base_url}"
            )
        return AIProviderError(f"Local provider API error: {error}")

    def analyze_with_progress(
        self,
        system_prompt: str,
        user_prompt: str,
        progress_callback: Callable[[dict[str, str]], None] | None,
        attachments: list[Mapping[str, str]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with streamed progress updates when supported.

        Streams the response and periodically invokes ``progress_callback``
        with accumulated thinking and answer text. Falls back to
        ``analyze_with_attachments`` when no callback is provided.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            progress_callback: Optional callable receiving progress dicts.
            attachments: Optional list of attachment descriptors.
            max_tokens: Maximum completion tokens.

        Returns:
            The generated analysis text with reasoning blocks removed.

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

        attachment_response = self._run_request(
            lambda: self._request_with_csv_attachments(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                attachments=attachments,
            )
        )
        if attachment_response:
            cleaned_response = self._clean_openai_compatible_response_text(attachment_response)
            if cleaned_response:
                return cleaned_response
            self._raise_openai_compatible_empty_response(
                None,
                "Local AI provider returned an empty response",
            )

        prompt_for_completion = self._build_chat_completion_prompt(
            user_prompt=user_prompt,
            attachments=attachments,
        )
        messages = self._chat_completion_messages(system_prompt, prompt_for_completion)

        def _stream_factory() -> Any:
            """Open a progress stream or fall back to a non-streamed answer."""
            try:
                return self._create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=True,
                )
            except self._openai.BadRequestError as error:
                if self._is_stream_unsupported_error(error):
                    return self._request_non_stream(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        max_tokens=max_tokens,
                        attachments=attachments,
                    )
                raise

        thinking_parts: list[str] = []
        answer_parts: list[str] = []

        def _progress_chunks(stream: Any) -> Iterator[Any]:
            """Emit progress while yielding answer/reasoning chunks for retry tracking."""
            return stream_progress_chunks(
                chunks=self._iter_openai_compatible_stream_text(stream),
                progress_callback=progress_callback,
                thinking_parts=thinking_parts,
                answer_parts=answer_parts,
            )

        stream = _run_stream_with_rate_limit_retries(
            stream_factory=_stream_factory,
            stream_text_iterator=_progress_chunks,
            rate_limit_error_type=self._openai.RateLimitError,
            provider_name=self._provider_display_name,
            map_error=self._map_api_error,
            empty_response_message=(
                "Local AI provider returned an empty streamed response. "
                "Try a different local model or increase max tokens."
            ),
        )
        for _chunk in stream:
            pass
        return self._finalize_stream_response(thinking_parts, answer_parts)

    # Class-level alias retained so existing callers and unit tests that
    # exercise the shared progress throttle via ``LocalProvider`` keep working.
    _emit_progress_if_needed = staticmethod(emit_progress_if_needed)

    @staticmethod
    def _finalize_stream_response(
        thinking_parts: list[str],
        answer_parts: list[str],
    ) -> str:
        """Assemble the final response text from accumulated stream parts.

        Args:
            thinking_parts: Collected thinking-channel text fragments.
            answer_parts: Collected answer-channel text fragments.

        Returns:
            The cleaned final answer.

        Raises:
            AIProviderError: If both channels are empty.
        """
        return finalize_progress_stream_response(
            thinking_parts,
            answer_parts,
            empty_response_message=(
                "Local AI provider returned an empty streamed response. "
                "Try a different local model or increase max tokens."
            ),
        )

    def _clean_openai_compatible_response_text(self, text: str) -> str:
        """Strip leading local-model reasoning blocks from completed text."""
        return _strip_leading_reasoning_blocks(text)

    def _raise_openai_compatible_empty_response(self, response: Any, message: str) -> None:
        """Raise the local empty-response error with finish-reason detail."""
        finish_reason = None
        choices = getattr(response, "choices", None)
        if choices:
            first_choice = choices[0]
            finish_reason = getattr(first_choice, "finish_reason", None)
            if finish_reason is None and isinstance(first_choice, dict):
                finish_reason = first_choice.get("finish_reason")
        reason_detail = f" (finish_reason={finish_reason})" if finish_reason else ""
        raise AIProviderError(f"{message}{reason_detail}. This can happen with reasoning-only outputs or very low token limits.")

    def _request_non_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None = None,
    ) -> str:
        """Perform a non-streaming local request with attachment handling.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt text.
            max_tokens: Maximum completion tokens.
            attachments: Optional list of attachment descriptors.

        Returns:
            The generated analysis text with reasoning blocks removed.

        Raises:
            AIProviderError: If the response is empty.
        """
        return self._request_openai_compatible_non_stream(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            attachments=attachments,
            empty_response_message="Local AI provider returned an empty response",
        )

    def _create_chat_completion(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        stream: bool = False,
    ) -> Any:
        """Create a local OpenAI-compatible chat completion with token retry."""
        return self._create_openai_compatible_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            stream=stream,
        )

    @staticmethod
    def _is_stream_unsupported_error(error: Exception) -> bool:
        """Return true when a local endpoint rejects streaming mode."""
        lowered_error = str(error).lower()
        return "stream" in lowered_error and (
            "unsupported" in lowered_error or "not support" in lowered_error
        )

    def _build_chat_completion_prompt(
        self,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
    ) -> str:
        """Build the user prompt, inlining attachments if needed.

        Args:
            user_prompt: The original user-facing prompt text.
            attachments: Optional list of attachment descriptors.

        Returns:
            The prompt string, potentially with attachment data appended.
        """
        return self._build_openai_compatible_prompt(user_prompt, attachments)

    def _request_with_csv_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None,
    ) -> str | None:
        """Attempt to send a request with CSV files via the local Responses API.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt text.
            max_tokens: Maximum completion tokens.
            attachments: Optional list of attachment descriptors.

        Returns:
            The generated text if succeeded, or ``None`` if skipped.
        """
        return self._request_with_openai_compatible_csv_attachments(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            attachments=attachments,
        )

    def get_model_info(self) -> dict[str, str]:
        """Return local provider and model metadata.

        Returns:
            A dictionary with ``"provider"`` and ``"model"`` keys.
        """
        return {"provider": "local", "model": self.model}
