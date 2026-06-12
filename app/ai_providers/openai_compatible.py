"""Shared helpers for OpenAI-compatible chat-completion providers.

OpenAI, Kimi, and local endpoints all expose the same Chat Completions
shape for normal requests, streaming deltas, progress-mode streaming with
throttled GUI callbacks, token-limit retries, and the file-attachment
fallback ladder.  This module keeps that plumbing in one place while
provider classes keep their endpoint, model, token-parameter,
upload-purpose, and user-facing message choices explicit.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator, Mapping

from .base import (
    AIProviderError,
    DEFAULT_MAX_TOKENS,
    _is_attachment_unsupported_error,
    _is_unsupported_parameter_error,
    _run_stream_with_rate_limit_retries,
    _run_with_completion_token_retry,
)
from .progress import (
    finalize_progress_stream_response,
    stream_progress_chunks,
)
from .utils import (
    _LeadingReasoningStreamSplitter,
    StreamedResponseChunk,
    _extract_openai_stream_chunk_delta,
    _extract_openai_text,
    _inline_attachment_data_into_prompt,
    _split_openai_stream_delta_text,
    stream_chunk_has_text,
    upload_and_request_via_responses_api,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleChatMixin:
    """Mixin for providers backed by OpenAI-compatible chat completions.

    Provider classes are expected to expose ``client``, ``model``, ``_openai``,
    ``_map_api_error()``, ``_run_request()``, and ``_prepare_csv_attachments()``
    from ``AIProvider``.  The class attributes below are intentionally plain
    and explicit so vendor-specific behavior stays visible in each provider.
    """

    _openai_compatible_token_parameters: tuple[str, ...] = ("max_tokens",)
    _responses_provider_name: str = "OpenAI-compatible provider"
    _responses_upload_purpose: str = "assistants"
    _responses_convert_csv_to_txt: bool = False
    _stream_splits_leading_reasoning: bool = False

    def _chat_completion_messages(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> list[dict[str, str]]:
        """Build the two-message Chat Completions payload."""
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _create_openai_compatible_chat_completion(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        stream: bool = False,
    ) -> Any:
        """Create a chat completion with shared token-parameter retry logic.

        The configured token parameters are tried in order.  OpenAI uses
        ``max_completion_tokens`` first and falls back to ``max_tokens`` only
        when the endpoint explicitly rejects the newer parameter.  Kimi and
        local endpoints keep the single ``max_tokens`` path.
        """
        token_parameters = tuple(self._openai_compatible_token_parameters) or ("max_tokens",)
        last_index = len(token_parameters) - 1
        for index, token_parameter in enumerate(token_parameters):
            request_kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                token_parameter: max_tokens,
            }
            if stream:
                request_kwargs["stream"] = True

            try:
                return _run_with_completion_token_retry(
                    create_fn=lambda kw: self.client.chat.completions.create(**kw),
                    request_kwargs=request_kwargs,
                    token_parameter=token_parameter,
                    bad_request_error_type=self._openai.BadRequestError,
                    provider_name=self._provider_display_name,
                )
            except self._openai.BadRequestError as error:
                is_last_parameter = index >= last_index
                if is_last_parameter or not _is_unsupported_parameter_error(error, token_parameter):
                    raise

        raise AIProviderError(
            f"{self._provider_display_name} could not select a supported token parameter."
        )

    def _iter_openai_compatible_stream_text(
        self,
        stream: Any,
        *,
        split_leading_reasoning: bool | None = None,
    ) -> Iterator[Any]:
        """Yield separated answer/reasoning chunks from a stream response."""
        if isinstance(stream, str):
            if stream:
                yield StreamedResponseChunk(answer_text=stream)
            return

        split_reasoning = (
            self._stream_splits_leading_reasoning
            if split_leading_reasoning is None
            else bool(split_leading_reasoning)
        )
        content_splitter = _LeadingReasoningStreamSplitter() if split_reasoning else None

        for chunk in stream:
            delta = _extract_openai_stream_chunk_delta(chunk)
            if delta is None:
                continue

            delta_text = _split_openai_stream_delta_text(delta)
            if content_splitter is not None:
                answer_split = content_splitter.split(delta_text.answer_text)
                delta_text = StreamedResponseChunk(
                    answer_text=answer_split.answer_text,
                    reasoning_text=delta_text.reasoning_text + answer_split.reasoning_text,
                )

            if stream_chunk_has_text(delta_text):
                yield delta_text

        if content_splitter is not None:
            remaining_text = content_splitter.flush()
            if stream_chunk_has_text(remaining_text):
                yield remaining_text

    def _stream_openai_compatible_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        prompt_for_completion: str | None = None,
        stream_factory: Any | None = None,
        empty_response_message: str,
    ) -> Iterator[Any]:
        """Run a streaming chat completion with shared rate-limit behavior."""
        completion_prompt = user_prompt if prompt_for_completion is None else prompt_for_completion
        messages = self._chat_completion_messages(system_prompt, completion_prompt)

        if stream_factory is None:
            def stream_factory() -> Any:
                """Open the provider streaming chat completion."""
                return self._create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=True,
                )

        return _run_stream_with_rate_limit_retries(
            stream_factory=stream_factory,
            stream_text_iterator=self._iter_openai_compatible_stream_text,
            rate_limit_error_type=self._openai.RateLimitError,
            provider_name=self._provider_display_name,
            map_error=self._map_api_error,
            empty_response_message=empty_response_message,
        )

    def _analyze_with_progress_via_chat_stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        progress_callback: Callable[[dict[str, str]], None],
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int,
        prompt_for_completion: str | None = None,
        stream_factory: Callable[[], Any] | None = None,
        empty_stream_message: str,
        empty_final_message: str,
    ) -> str:
        """Run a progress-mode streaming analysis over a chat completion.

        Implements the streaming flow shared by all OpenAI-compatible
        providers: inline attachment data into the prompt, build the
        two-message payload, open a streaming chat completion, forward
        throttled thinking/answer updates to ``progress_callback`` while
        draining the stream through the shared rate-limit retry plumbing,
        and validate the final answer text.

        Exceptions raised by ``progress_callback`` abort the stream and
        propagate unchanged — this is the analyzer's mid-stream
        cancellation mechanism.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt with investigation context.
            progress_callback: Callable receiving progress dicts with
                accumulated thinking and answer text.
            attachments: Optional list of attachment descriptors whose data
                is inlined into the streamed prompt.
            max_tokens: Maximum completion tokens.
            prompt_for_completion: Pre-built completion prompt with any
                attachment data already inlined. When ``None``, the prompt
                is built here from ``user_prompt`` and ``attachments``.
                Providers supplying a custom ``stream_factory`` that closes
                over an already-built payload pass the same prompt through
                so the request payload is built exactly once.
            stream_factory: Optional callable opening the provider stream.
                Defaults to a streaming ``_create_chat_completion`` call
                over the messages built here.
            empty_stream_message: User-facing error message raised when the
                stream completes without emitting any answer text.
            empty_final_message: User-facing error message raised when the
                accumulated answer channel is empty after cleaning.

        Returns:
            The cleaned final answer text.

        Raises:
            AIProviderError: On an empty streamed response or API failure.
            Exception: Any exception raised by ``progress_callback``,
                re-raised unchanged.
        """
        if prompt_for_completion is None:
            prompt_for_completion = self._build_openai_compatible_prompt(
                user_prompt=user_prompt,
                attachments=attachments,
            )

        if stream_factory is None:
            messages = self._chat_completion_messages(system_prompt, prompt_for_completion)

            def stream_factory() -> Any:
                """Open the provider streaming chat completion."""
                return self._create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=True,
                )

        thinking_parts: list[str] = []
        answer_parts: list[str] = []

        def _progress_chunks(stream: Any) -> Iterator[Any]:
            """Emit progress while yielding chunks for retry tracking."""
            return stream_progress_chunks(
                chunks=self._iter_openai_compatible_stream_text(stream),
                progress_callback=progress_callback,
                thinking_parts=thinking_parts,
                answer_parts=answer_parts,
            )

        stream = _run_stream_with_rate_limit_retries(
            stream_factory=stream_factory,
            stream_text_iterator=_progress_chunks,
            rate_limit_error_type=self._openai.RateLimitError,
            provider_name=self._provider_display_name,
            map_error=self._map_api_error,
            empty_response_message=empty_stream_message,
        )
        for _chunk in stream:
            pass
        return finalize_progress_stream_response(
            thinking_parts,
            answer_parts,
            empty_response_message=empty_final_message,
        )

    def _build_openai_compatible_prompt(
        self,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
    ) -> str:
        """Return a prompt with attachment data inlined when needed."""
        prompt_for_completion = user_prompt
        if attachments:
            prompt_for_completion, inlined_attachment_data = _inline_attachment_data_into_prompt(
                user_prompt=user_prompt,
                attachments=attachments,
            )
            if inlined_attachment_data:
                logger.info(
                    "%s attachment fallback inlined attachment data into prompt.",
                    self._provider_display_name,
                )
        return prompt_for_completion

    def _clean_openai_compatible_response_text(self, text: str) -> str:
        """Normalize non-streamed text before returning it to callers."""
        return str(text or "").strip()

    def _raise_openai_compatible_empty_response(self, response: Any, message: str) -> None:
        """Raise the provider-specific empty-response error."""
        del response
        raise AIProviderError(message)

    def _request_openai_compatible_non_stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None = None,
        empty_response_message: str,
    ) -> str:
        """Run the attachment ladder and final chat-completion request."""
        attachment_response = self._request_with_csv_attachments(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            attachments=attachments,
        )
        if attachment_response:
            cleaned_attachment_response = self._clean_openai_compatible_response_text(
                attachment_response
            )
            if cleaned_attachment_response:
                return cleaned_attachment_response
            self._raise_openai_compatible_empty_response(None, empty_response_message)

        prompt_for_completion = self._build_openai_compatible_prompt(
            user_prompt=user_prompt,
            attachments=attachments,
        )
        response = self._create_chat_completion(
            messages=self._chat_completion_messages(system_prompt, prompt_for_completion),
            max_tokens=max_tokens,
        )
        text = _extract_openai_text(response)
        if text:
            cleaned_text = self._clean_openai_compatible_response_text(text)
            if cleaned_text:
                return cleaned_text
            self._raise_openai_compatible_empty_response(response, empty_response_message)

        self._raise_openai_compatible_empty_response(response, empty_response_message)

    def _request_with_openai_compatible_csv_attachments(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None,
    ) -> str | None:
        """Attempt Responses API file-attachment mode for CSV evidence."""
        normalized_attachments = self._prepare_csv_attachments(
            attachments,
            supports_file_attachments=hasattr(self.client, "files") and hasattr(self.client, "responses"),
        )
        if not normalized_attachments:
            return None

        try:
            text = upload_and_request_via_responses_api(
                client=self.client,
                openai_module=self._openai,
                model=self.model,
                normalized_attachments=normalized_attachments,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                provider_name=self._responses_provider_name,
                upload_purpose=self._responses_upload_purpose,
                convert_csv_to_txt=self._responses_convert_csv_to_txt,
            )
            self._set_csv_attachment_supported(True)
            return text
        except Exception as error:
            if _is_attachment_unsupported_error(error, allow_bare_404=True):
                self._set_csv_attachment_supported(False)
                logger.info(
                    "%s endpoint does not support file attachments via /files + /responses; "
                    "falling back to chat.completions text mode.",
                    self._provider_display_name,
                )
                return None
            raise

    def _set_csv_attachment_supported(self, value: bool) -> None:
        """Update attachment support state while honoring provider locks."""
        attachment_lock = getattr(self, "_attachment_lock", None)
        if attachment_lock is None:
            self._csv_attachment_supported = value
            return
        with attachment_lock:
            self._csv_attachment_supported = value
