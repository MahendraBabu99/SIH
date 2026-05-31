"""Moonshot Kimi API provider implementation.

Uses the ``openai`` Python SDK pointed at the Moonshot Kimi API base URL.
Supports synchronous and streaming generation, CSV file attachments via
the Responses API, and automatic model-alias mapping for deprecated Kimi
model identifiers.

Attributes:
    logger: Module-level logger for Kimi provider operations.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterator, Mapping

from .base import (
    AIProvider,
    AIProviderError,
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_KIMI_BASE_URL,
    DEFAULT_KIMI_FILE_UPLOAD_PURPOSE,
    DEFAULT_KIMI_MODEL,
    DEFAULT_MAX_TOKENS,
    _is_kimi_model_not_available_error,
    _normalize_api_key_value,
    _normalize_kimi_model_name,
    _normalize_openai_compatible_base_url,
    _resolve_timeout_seconds,
)
from .openai_compatible import OpenAICompatibleChatMixin

logger = logging.getLogger(__name__)


class KimiProvider(OpenAICompatibleChatMixin, AIProvider):
    """Moonshot Kimi API provider implementation.

    Attributes:
        _api_key (str): The Moonshot/Kimi API key (private to reduce
            accidental exposure in repr/debug output).
        model (str): The Kimi model identifier.
        base_url (str): The normalized Kimi API base URL.
        attach_csv_as_file (bool): Whether to upload CSV artifacts as
            file attachments.
        request_timeout_seconds (float): HTTP timeout in seconds.
        client: The ``openai.OpenAI`` SDK client instance configured for Kimi.
    """

    _provider_display_name: str = "Kimi"
    _openai_compatible_token_parameters = ("max_tokens",)
    _responses_provider_name = "Kimi"
    _responses_upload_purpose = DEFAULT_KIMI_FILE_UPLOAD_PURPOSE
    _responses_convert_csv_to_txt = False

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_KIMI_MODEL,
        base_url: str = DEFAULT_KIMI_BASE_URL,
        attach_csv_as_file: bool = True,
        request_timeout_seconds: float = DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the Kimi provider.

        Args:
            api_key: Moonshot/Kimi API key. Must be non-empty.
            model: Kimi model identifier. Deprecated aliases are mapped.
            base_url: Kimi API base URL.
            attach_csv_as_file: If ``True``, attempt file uploads.
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
                "Kimi API key is not configured. "
                "Set `ai.kimi.api_key` in config/config.yaml or the "
                "MOONSHOT_API_KEY or KIMI_API_KEY environment variable."
            )

        self._openai = openai
        self._api_key = normalized_api_key
        self.model = _normalize_kimi_model_name(model)
        self.base_url = _normalize_openai_compatible_base_url(
            base_url=base_url,
            default_base_url=DEFAULT_KIMI_BASE_URL,
        )
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
            base_url=self.base_url,
            timeout=self.request_timeout_seconds,
        )
        logger.info("Initialized Kimi provider at %s with model %s (timeout %.1fs)", self.base_url, self.model, self.request_timeout_seconds)

    def _map_api_error(self, error: Exception) -> AIProviderError:
        """Map an OpenAI SDK exception to an ``AIProviderError`` with Kimi messages.

        Overrides the base implementation to provide Kimi-specific error
        messages (authentication config keys, model-not-available detection).

        Args:
            error: The raw SDK or network exception.

        Returns:
            An ``AIProviderError`` with a user-friendly message.
        """
        if isinstance(error, self._openai.APIConnectionError):
            return AIProviderError(
                "Unable to connect to Kimi API. Check `ai.kimi.base_url` and network access."
            )
        if isinstance(error, self._openai.AuthenticationError):
            return AIProviderError(
                "Kimi authentication failed. Check `ai.kimi.api_key`, MOONSHOT_API_KEY, or KIMI_API_KEY."
            )
        if isinstance(error, self._openai.APIError):
            if _is_kimi_model_not_available_error(error):
                return AIProviderError(
                    "Kimi rejected the configured model. "
                    f"Current model: `{self.model}`. "
                    "Set `ai.kimi.model` to a model enabled for your Moonshot account "
                    "(for example `kimi-k2-turbo-preview`) and retry."
                )
        return super()._map_api_error(error)

    def analyze_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[str]:
        """Stream generated chunks from Kimi.

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
            empty_response_message="Kimi returned an empty response.",
        )

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[Mapping[str, str]] | None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Analyze with optional CSV file attachments via the Kimi Responses API.

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
            """Run the non-streaming Kimi request.

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

    def _request_non_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        attachments: list[Mapping[str, str]] | None = None,
    ) -> str:
        """Perform a non-streaming Kimi request with attachment handling.

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
            empty_response_message="Kimi returned an empty response.",
        )

    def _create_chat_completion(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        stream: bool = False,
    ) -> Any:
        """Create a Kimi chat completion with token-cap retry parity."""
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
        """Attempt to send a request with CSV files via the Kimi Responses API.

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
        """Return Kimi provider and model metadata.

        Returns:
            A dictionary with ``"provider"`` and ``"model"`` keys.
        """
        return {"provider": "kimi", "model": self.model}
