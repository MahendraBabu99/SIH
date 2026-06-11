"""Tests for the Kimi AI provider implementation."""
from __future__ import annotations

import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from app.ai_providers import (
    AIProviderError,
    KimiProvider,
    DEFAULT_KIMI_MODEL,
    DEFAULT_KIMI_FILE_UPLOAD_PURPOSE,
)
from app.ai_providers.base import (
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOKENS,
    _RATE_LIMIT_STATE,
    RATE_LIMIT_MAX_RETRIES,
)


def _make_openai_response(text: str) -> SimpleNamespace:
    """Build a minimal OpenAI-style chat completion response."""
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _raise_after_chunks(chunks: list[SimpleNamespace], error: Exception):
    """Yield stream chunks and then raise an error."""
    yield from chunks
    raise error


class _CallbackAbortError(Exception):
    """Custom exception raised by test progress callbacks to abort streams."""


# ---------------------------------------------------------------------------
# KimiProvider
# ---------------------------------------------------------------------------

class TestKimiProvider(unittest.TestCase):
    @patch("openai.OpenAI")
    def test_missing_api_key_message_mentions_supported_env_vars(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        with self.assertRaises(AIProviderError) as ctx:
            KimiProvider(api_key="")

        self.assertIn("MOONSHOT_API_KEY", str(ctx.exception))
        self.assertIn("KIMI_API_KEY", str(ctx.exception))
        mock_openai_cls.assert_not_called()

    @patch("openai.OpenAI")
    def test_analyze_returns_text(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "Kimi result"
        )

        provider = KimiProvider(
            api_key="sk-test",
            model=DEFAULT_KIMI_MODEL,
            base_url="https://api.moonshot.ai/v1",
        )
        result = provider.analyze("system", "user")
        self.assertEqual(result, "Kimi result")

    @patch("openai.OpenAI")
    def test_analyze_stream_yields_text_chunks(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Chunk 1 "))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Chunk 2"))]),
        ]

        provider = KimiProvider(
            api_key="sk-test",
            model=DEFAULT_KIMI_MODEL,
            base_url="https://api.moonshot.ai/v1",
        )
        chunks = list(provider.analyze_stream("system", "user"))

        self.assertEqual(chunks, ["Chunk 1 ", "Chunk 2"])
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["max_tokens"], 16384)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_streams_reasoning_content(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(reasoning_content="Kimi thinking. "),
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="Final answer."),
                    )
                ]
            ),
        ]

        progress_updates: list[dict[str, str]] = []
        provider = KimiProvider(api_key="sk-test")
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda payload: progress_updates.append(payload),
        )

        self.assertEqual(result, "Final answer.")
        self.assertTrue(progress_updates)
        self.assertEqual(progress_updates[-1]["status"], "thinking")
        self.assertIn("Kimi thinking.", progress_updates[-1]["thinking_text"])
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])

    @patch("openai.OpenAI")
    def test_analyze_with_progress_callback_exception_aborts_stream(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """A raising progress callback aborts the stream mid-flight."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(reasoning_content="Kimi thinking. "),
                    )
                ]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Partial answer. "))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Rest of answer."))]
            ),
        ]
        pulled: list[int] = []

        def _recording_stream():
            """Yield mocked stream chunks while recording consumption."""
            for index, chunk in enumerate(chunks):
                pulled.append(index)
                yield chunk

        mock_client.chat.completions.create.return_value = _recording_stream()

        def cancelling_callback(_payload: dict[str, str]) -> None:
            """Support test behavior for cancelling_callback."""
            raise _CallbackAbortError("cancel requested")

        provider = KimiProvider(api_key="sk-test")
        with self.assertRaises(_CallbackAbortError):
            provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=cancelling_callback,
            )

        self.assertEqual(pulled, [0])
        _RATE_LIMIT_STATE.pop("Kimi", None)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_reads_reasoning_content_from_model_extra(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        reasoning_delta = SimpleNamespace(content=None)
        reasoning_delta.model_extra = {"reasoning_content": "extra reasoning"}
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=reasoning_delta)]),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Final answer."))]
            ),
        ]

        progress_updates: list[dict[str, str]] = []
        provider = KimiProvider(api_key="sk-test")
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda payload: progress_updates.append(payload),
        )

        self.assertEqual(result, "Final answer.")
        self.assertIn("extra reasoning", progress_updates[-1]["thinking_text"])

    @patch("openai.OpenAI")
    def test_analyze_with_progress_inlines_attachments_for_streaming(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="Streamed result."))
                ]
            )
        ]

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [
                {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
            ]

            provider = KimiProvider(api_key="sk-test")
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda _payload: None,
                attachments=attachments,
            )

        self.assertEqual(result, "Streamed result.")
        self.assertEqual(mock_client.files.create.call_count, 0)
        self.assertEqual(mock_client.responses.create.call_count, 0)
        stream_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertTrue(stream_kwargs["stream"])
        stream_prompt = stream_kwargs["messages"][1]["content"]
        self.assertIn("File attachments were unavailable", stream_prompt)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", stream_prompt)
        self.assertIn("ts,name", stream_prompt)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_no_callback_delegates_to_attachment_mode(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "Kimi non-progress result"
        )

        provider = KimiProvider(api_key="sk-test")
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=None,
        )

        self.assertEqual(result, "Kimi non-progress result")
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("stream", kwargs)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_rate_limit_after_reasoning_does_not_retry(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        class _FakeRateLimitError(Exception):
            """Fake rate-limit error for progress retry tests."""

        _RATE_LIMIT_STATE.pop("Kimi", None)
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = _FakeRateLimitError("rate limited after reasoning")
        mock_client.chat.completions.create.return_value = _raise_after_chunks(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                reasoning_content="Kimi thinking before failure"
                            )
                        )
                    ]
                ),
            ],
            error,
        )
        progress_updates: list[dict[str, str]] = []

        with patch("openai.RateLimitError", _FakeRateLimitError), patch(
            "app.ai_providers.base.time.sleep"
        ) as mock_sleep:
            provider = KimiProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze_with_progress(
                    "system",
                    "user",
                    progress_callback=progress_updates.append,
                )

        self.assertIn("partial output", str(ctx.exception))
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)
        self.assertFalse(mock_sleep.called)
        self.assertTrue(
            any(
                "Kimi thinking before failure" in item.get("thinking_text", "")
                for item in progress_updates
            )
        )
        _RATE_LIMIT_STATE.pop("Kimi", None)

    @patch("openai.OpenAI")
    def test_analyze_stream_empty_response_raises(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
        ]

        provider = KimiProvider(api_key="sk-test")
        with self.assertRaises(AIProviderError) as ctx:
            list(provider.analyze_stream("system", "user"))
        self.assertIn("empty response", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_stream_refusal_raises(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(refusal="No analysis"))]),
        ]

        provider = KimiProvider(api_key="sk-test")
        with self.assertRaises(AIProviderError) as ctx:
            list(provider.analyze_stream("system", "user"))
        self.assertIn("refused", str(ctx.exception))
        self.assertIn("No analysis", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        class _FakeBadRequestError(Exception):
            def __init__(self, message: str, *, param: str | None = None) -> None:
                super().__init__(message)
                self.param = param
                self.body = {"error": {"message": message, "param": param}}

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _FakeBadRequestError(
                "maxtokens is too large: 256000. This model supports at most 128000 completion tokens.",
                param="maxtokens",
            ),
            _make_openai_response("Kimi capped result"),
        ]

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = KimiProvider(api_key="sk-test")
            result = provider.analyze("system", "user", max_tokens=256000)

        self.assertEqual(result, "Kimi capped result")
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)

    @patch("openai.OpenAI")
    def test_analyze_stream_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        class _FakeBadRequestError(Exception):
            def __init__(self, message: str, *, param: str | None = None) -> None:
                super().__init__(message)
                self.param = param
                self.body = {"error": {"message": message, "param": param}}

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _FakeBadRequestError(
                "maxtokens is too large: 256000. This model supports at most 128000 completion tokens.",
                param="maxtokens",
            ),
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Kimi capped stream"))]
                )
            ],
        ]

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = KimiProvider(api_key="sk-test")
            chunks = list(provider.analyze_stream("system", "user", max_tokens=256000))

        self.assertEqual(chunks, ["Kimi capped stream"])
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)
        self.assertTrue(second_kwargs["stream"])

    @patch("openai.OpenAI")
    def test_get_model_info(self, _mock: MagicMock) -> None:
        provider = KimiProvider(
            api_key="sk-test",
            model=DEFAULT_KIMI_MODEL,
            base_url="https://api.moonshot.ai/v1",
        )
        info = provider.get_model_info()
        self.assertEqual(info["provider"], "kimi")
        self.assertEqual(info["model"], DEFAULT_KIMI_MODEL)

    @patch("openai.OpenAI")
    def test_normalizes_deprecated_model_alias(self, _mock: MagicMock) -> None:
        provider = KimiProvider(
            api_key="sk-test",
            model="kimi-v2.5",
            base_url="https://api.moonshot.ai/v1",
        )
        self.assertEqual(provider.model, DEFAULT_KIMI_MODEL)

    def test_rejects_empty_api_key(self) -> None:
        with self.assertRaises(AIProviderError) as ctx:
            KimiProvider(api_key="")
        self.assertIn("API key is not configured", str(ctx.exception))

    def test_rejects_whitespace_api_key(self) -> None:
        with self.assertRaises(AIProviderError) as ctx:
            KimiProvider(api_key="   ")
        self.assertIn("API key is not configured", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_uses_responses_api_when_supported(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-123")
        mock_client.responses.create.return_value = SimpleNamespace(output_text="Kimi attachment result")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = KimiProvider(
                api_key="sk-test",
                model=DEFAULT_KIMI_MODEL,
                base_url="https://api.moonshot.ai/v1",
            )
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}],
            )

        self.assertEqual(result, "Kimi attachment result")
        self.assertEqual(mock_client.files.create.call_count, 1)
        self.assertEqual(
            mock_client.files.create.call_args.kwargs["purpose"],
            DEFAULT_KIMI_FILE_UPLOAD_PURPOSE,
        )
        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertEqual(mock_client.files.delete.call_count, 1)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_falls_back_when_endpoint_unsupported(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-unsupported")
        mock_client.responses.create.side_effect = RuntimeError("unrecognized request url /responses")
        mock_client.chat.completions.create.return_value = _make_openai_response("Kimi fallback result")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}]

            provider = KimiProvider(
                api_key="sk-test",
                model=DEFAULT_KIMI_MODEL,
                base_url="https://api.moonshot.ai/v1",
            )
            first_result = provider.analyze_with_attachments("system", "user", attachments=attachments)
            second_result = provider.analyze_with_attachments("system", "user", attachments=attachments)

        self.assertEqual(first_result, "Kimi fallback result")
        self.assertEqual(second_result, "Kimi fallback result")
        self.assertEqual(mock_client.files.create.call_count, 1)
        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertGreaterEqual(mock_client.chat.completions.create.call_count, 2)
        first_prompt = mock_client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
        second_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
        self.assertIn("File attachments were unavailable", first_prompt)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", first_prompt)
        self.assertIn("ts,name", first_prompt)
        self.assertIn("File attachments were unavailable", second_prompt)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", second_prompt)
        self.assertIn("ts,name", second_prompt)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_propagates_unrelated_bad_request(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        class _FakeBadRequestError(Exception):
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-related")
        mock_client.responses.create.side_effect = _FakeBadRequestError(
            "this model does not support max_tokens"
        )
        mock_client.chat.completions.create.return_value = _make_openai_response("should not fallback")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            with patch("openai.BadRequestError", _FakeBadRequestError):
                provider = KimiProvider(api_key="sk-test")
                with self.assertRaises(AIProviderError) as ctx:
                    provider.analyze_with_attachments(
                        "system",
                        "user",
                        attachments=[{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}],
                    )

        self.assertIn("request was rejected", str(ctx.exception))
        self.assertIsNone(provider._csv_attachment_supported)
        self.assertEqual(mock_client.chat.completions.create.call_count, 0)

    @patch("openai.OpenAI")
    def test_analyze_connection_error(self, mock_openai_cls: MagicMock) -> None:
        class _FakeAPIConnectionError(Exception):
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = _FakeAPIConnectionError("connection refused")

        with patch("openai.APIConnectionError", _FakeAPIConnectionError):
            provider = KimiProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("Unable to connect", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_auth_error(self, mock_openai_cls: MagicMock) -> None:
        class _FakeAuthError(Exception):
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = _FakeAuthError("invalid key")

        with patch("openai.AuthenticationError", _FakeAuthError):
            provider = KimiProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("authentication failed", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_model_not_available_error(self, mock_openai_cls: MagicMock) -> None:
        class _FakeAPIError(Exception):
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = _FakeAPIError("model not found: kimi-custom")

        with patch("openai.APIError", _FakeAPIError):
            provider = KimiProvider(api_key="sk-test", model="kimi-custom")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("rejected the configured model", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_empty_response_raises(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[])

        provider = KimiProvider(api_key="sk-test")
        with self.assertRaises(AIProviderError) as ctx:
            provider.analyze("system", "user")
        self.assertIn("empty response", str(ctx.exception))


# ---------------------------------------------------------------------------
# LocalProvider
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
