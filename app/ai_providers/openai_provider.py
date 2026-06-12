"""OpenAI API provider implementation.

Uses the ``openai`` Python SDK to communicate with the OpenAI Chat
Completions and Responses APIs. Supports synchronous and streaming
generation, streamed progress mode with live partial answer text, CSV
file attachments via the Responses API, and automatic fallback between
``max_completion_tokens`` and ``max_tokens`` parameters.

Attributes:
    logger: Module-level logger for OpenAI provider operations.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterator, Mapping

from .base import (
    AIProvider,
    AIProviderError,
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OPENAI_MODEL,
    _normalize_api_key_value,
    _resolve_timeout_seconds,
)
from .openai_compatible import OpenAICompatibleChatMixin

logger = logging.getLogger(__name__)


class OpenAIProvider(OpenAICompatibleChatMixin, AIProvider):
    """OpenAI API provider implementation.

    Attributes:
        _api_key (str): The OpenAI API key (private to reduce
            accidental exposure in repr/debug output).
        model (str): The OpenAI model identifier.
        attach_csv_as_file (bool): Whether to upload CSV artifacts as
            file attachments via the Responses API.
        request_timeout_seconds (float): HTTP timeout in seconds.
        client: The ``openai.OpenAI`` SDK client instance.
    """

    _provider_display_name: str = "OpenAI"
    _openai_compatible_token_parameters = ("max_completion_tokens", "max_tokens")
    _responses_provider_name = "OpenAI"
    _responses_upload_purpose = "assistants"
    _responses_convert_csv_to_txt = True

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        attach_csv_as_file: bool = True,
        request_timeout_seconds: float = DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the OpenAI provider.

        Args:
            api_key: OpenAI API key. Must be non-empty.
            model: OpenAI model identifier.
            attach_csv_as_file: If ``True``, attempt file uploads via
                the Responses API.
            request_timeout_seconds: HTTP timeout in seconds.

        Raises:
            AIProviderError: If the ``openai`` SDK is not installed or
                the API key is empty.
        """
        try:
            import openai
        except ImportError as error:
            raise AIProviderError(
                "openai SDK is not installed. Install it with `pip install openai`."
            ) from error

        normalized_api_key = _normalize_api_key_value(api_key)
        if not normalized_api_key:
            raise AIProviderError(
                "OpenAI API key is not configured. "
                "Set `ai.openai.api_key` in config/config.yaml or the OPENAI_API_KEY environment variable."
            )

        self._openai = openai
        self._api_key = normalized_api_key
        self.model = model
        self.attach_csv_as_file = bool(attach_csv_as_file)
        self._csv_attachment_supported: bool | None = None
        self._attachment_lock = threading.Lock()
        self._rate_limit_error_class = openai.RateLimitError
        self.request_timeout_seconds = _resolve_timeout_seconds(
            request_timeout_seconds,
            DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
        )
        self.client = openai.OpenAI(
            api_key=normalized_api_key,
            timeout=self.request_timeout_seconds,
        )
        logger.info("Initialized OpenAI provider with model %s (timeout %.1fs)", model, self.request_timeout_seconds)

    def _map_api_error(self, error: Exception) -> AIProviderError:
        """Map an OpenAI SDK exception to an ``AIProviderError``.

        Overrides the base implementation to provide OpenAI-specific error
        messages referencing the correct config keys.

        Args:
            error: The raw SDK or network exception.

        Returns:
            An ``AIProviderError`` with a user-friendly message.
        """
        if isinstance(error, self._openai.AuthenticationError):
            return AIProviderError(
                "OpenAI authentication failed. Check `ai.openai.api_key` or OPENAI_API_KEY."
            )
        return super()._map_api_error(error)

    def analyze_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[str]:
        """Stream generated chunks from OpenAI.

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
        return self._stream_openai_compatible_chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            empty_response_message="OpenAI returned an empty response.",
        )

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with optional CSV file attachments via the Responses API.

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
            """Run the non-streaming OpenAI request.

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

    def analyze_with_progress(
        self,
        system_prompt: str,
        user_prompt: str,
        progress_callback: Callable[[dict[str, str]], None] | None,
        attachments: list[Mapping[str, str]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with streamed partial answer text for live GUI progress.

        Progress mode streams the Chat Completions response and periodically
        invokes ``progress_callback`` with accumulated answer text (and
        thinking text when the endpoint exposes a reasoning channel). CSV
        attachment data is inlined into the streamed prompt instead of being
        uploaded via the Responses API so partial output stays visible.
        Falls back to ``analyze_with_attachments`` when no callback is
        provided.

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

        return self._analyze_with_progress_via_chat_stream(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            progress_callback=progress_callback,
            attachments=attachments,
            max_tokens=max_tokens,
            empty_stream_message=(
                "OpenAI returned an empty streamed response. "
                "Try increasing max tokens."
            ),
            empty_final_message=(
                "OpenAI returned an empty streamed response. "
                "This can happen with reasoning-only outputs or very low token limits."
            ),
        )

    def _request_non_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None = None,
    ) -> str:
        """Perform a non-streaming OpenAI request with attachment handling.

        Tries file-attachment mode first, then falls back to inlining
        attachment data, and finally issues a plain Chat Completions request.

        Args:
            system_prompt: The system-level instruction text.
            user_prompt: The user-facing prompt text.
            max_tokens: Maximum completion tokens.
            attachments: Optional list of attachment descriptors.

        Returns:
            The generated analysis text.

        Raises:
            AIProviderError: If the response is empty.
        """
        return self._request_openai_compatible_non_stream(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            attachments=attachments,
            empty_response_message="OpenAI returned an empty response.",
        )

    def _create_chat_completion(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        stream: bool = False,
    ) -> Any:
        """Create a Chat Completions request with token parameter fallback.

        Tries ``max_completion_tokens`` first, then falls back to
        ``max_tokens`` if the endpoint reports the parameter as unsupported.
        Also retries with a reduced token count when the provider rejects
        the requested maximum.

        Args:
            messages: The conversation messages list.
            max_tokens: Maximum completion tokens.
            stream: If ``True``, return a streaming response iterator.

        Returns:
            The OpenAI ``ChatCompletion`` response or streaming iterator.
        """
        return self._create_openai_compatible_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            stream=stream,
        )

    def _request_with_csv_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None,
    ) -> str | None:
        """Attempt to send a request with CSV files via the Responses API.

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
        """Return OpenAI provider and model metadata.

        Returns:
            A dictionary with ``"provider"`` and ``"model"`` keys.
        """
        return {"provider": "openai", "model": self.model}
