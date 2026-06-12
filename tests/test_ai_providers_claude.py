"""Tests for the Claude AI provider implementation."""
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
    ClaudeProvider,
)
from app.ai_providers.base import (
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOKENS,
    _RATE_LIMIT_STATE,
    RATE_LIMIT_MAX_RETRIES,
)
from app.ai_providers.utils import _extract_anthropic_text


def _make_anthropic_response(text: str) -> SimpleNamespace:
    """Build a minimal Anthropic-style response object."""
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


def _raise_after_chunks(chunks: list[SimpleNamespace], error: Exception):
    """Yield stream chunks and then raise an error."""
    yield from chunks
    raise error


class _CallbackAbortError(Exception):
    """Custom exception raised by test progress callbacks to abort streams."""


# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------

class TestClaudeProvider(unittest.TestCase):
    @patch("anthropic.Anthropic")
    def test_analyze_returns_text(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response(
            "Analysis result"
        )

        provider = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
        result = provider.analyze("system", "user")
        self.assertEqual(result, "Analysis result")

    @patch("anthropic.Anthropic")
    def test_analyze_stream_yields_text_chunks(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(text="Chunk 1 "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(text="Chunk 2"),
            ),
        ]

        provider = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
        chunks = list(provider.analyze_stream("system", "user"))

        self.assertEqual(chunks, ["Chunk 1 ", "Chunk 2"])
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_streams_thinking_and_returns_final_text(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="Claude thinking. "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Final answer."),
            ),
        ]

        progress_updates: list[dict[str, str]] = []
        provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda payload: progress_updates.append(payload),
        )

        self.assertEqual(result, "Final answer.")
        self.assertTrue(progress_updates)
        self.assertEqual(progress_updates[-1]["status"], "thinking")
        self.assertIn("Claude thinking.", progress_updates[-1]["thinking_text"])
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_callback_exception_aborts_stream(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        """A raising progress callback aborts the stream mid-flight."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        chunks = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="Claude thinking. "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Partial answer. "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Rest of answer."),
            ),
        ]
        pulled: list[int] = []

        def _recording_stream():
            """Yield mocked stream events while recording consumption."""
            for index, event in enumerate(chunks):
                pulled.append(index)
                yield event

        mock_client.messages.create.return_value = _recording_stream()

        def cancelling_callback(_payload: dict[str, str]) -> None:
            """Support test behavior for cancelling_callback."""
            raise _CallbackAbortError("cancel requested")

        provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
        with self.assertRaises(_CallbackAbortError):
            provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=cancelling_callback,
            )

        self.assertEqual(pulled, [0])
        _RATE_LIMIT_STATE.pop("Claude", None)

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_uses_attachment_blocks_for_streaming(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Streamed result."),
            ),
        ]

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [
                {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
            ]

            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda _payload: None,
                attachments=attachments,
            )

        self.assertEqual(result, "Streamed result.")
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        content = kwargs["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "text")
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", content[1]["text"])
        self.assertIn("ts,name", content[1]["text"])

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_falls_back_when_attachment_blocks_unsupported(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = [
            RuntimeError("unsupported document input"),
            [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="Inline fallback result."),
                ),
            ],
        ]

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [
                {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
            ]

            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda _payload: None,
                attachments=attachments,
            )

        self.assertEqual(result, "Inline fallback result.")
        self.assertEqual(mock_client.messages.create.call_count, 2)
        first_content = mock_client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
        second_content = mock_client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIsInstance(first_content, list)
        self.assertIsInstance(second_content, str)
        self.assertIn("File attachments were unavailable", second_content)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", second_content)
        self.assertIn("ts,name", second_content)
        self.assertFalse(provider._csv_attachment_supported)

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_no_callback_delegates_to_attachment_mode(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response(
            "Claude non-progress result"
        )

        provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=None,
        )

        self.assertEqual(result, "Claude non-progress result")
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertNotIn("stream", kwargs)

    @patch("anthropic.Anthropic")
    def test_analyze_with_progress_rate_limit_after_reasoning_does_not_retry(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        class _FakeRateLimitError(Exception):
            """Fake rate-limit error for progress retry tests."""

        _RATE_LIMIT_STATE.pop("Claude", None)
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        error = _FakeRateLimitError("rate limited after reasoning")
        mock_client.messages.create.return_value = _raise_after_chunks(
            [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(
                        type="thinking_delta",
                        thinking="Claude thinking before failure",
                    ),
                ),
            ],
            error,
        )
        progress_updates: list[dict[str, str]] = []

        with patch("anthropic.RateLimitError", _FakeRateLimitError), patch(
            "app.ai_providers.base.time.sleep"
        ) as mock_sleep:
            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze_with_progress(
                    "system",
                    "user",
                    progress_callback=progress_updates.append,
                )

        self.assertIn("partial output", str(ctx.exception))
        self.assertEqual(mock_client.messages.create.call_count, 1)
        self.assertFalse(mock_sleep.called)
        self.assertTrue(
            any(
                "Claude thinking before failure" in item.get("thinking_text", "")
                for item in progress_updates
            )
        )
        _RATE_LIMIT_STATE.pop("Claude", None)

    @patch("anthropic.Anthropic")
    def test_get_model_info(self, _mock: MagicMock) -> None:
        provider = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
        info = provider.get_model_info()
        self.assertEqual(info["provider"], "claude")
        self.assertEqual(info["model"], "claude-sonnet-4-20250514")

    def test_rejects_empty_api_key(self) -> None:
        with self.assertRaises(AIProviderError) as ctx:
            ClaudeProvider(api_key="")
        self.assertIn("API key is not configured", str(ctx.exception))

    def test_rejects_whitespace_api_key(self) -> None:
        with self.assertRaises(AIProviderError) as ctx:
            ClaudeProvider(api_key="   ")
        self.assertIn("API key is not configured", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_empty_response_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = SimpleNamespace(content=[])

        provider = ClaudeProvider(api_key="sk-test")
        with self.assertRaises(AIProviderError) as ctx:
            provider.analyze("system", "user")
        self.assertIn("empty response", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_analyze_stream_empty_response_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = [
            SimpleNamespace(type="message_stop"),
        ]

        provider = ClaudeProvider(api_key="sk-test")
        with self.assertRaises(AIProviderError) as ctx:
            list(provider.analyze_stream("system", "user"))
        self.assertIn("empty response", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_analyze_with_attachments_uses_document_blocks_when_supported(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response("Claude attachment result")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}],
            )

        self.assertEqual(result, "Claude attachment result")
        content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "text")
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", content[1]["text"])

    @patch("anthropic.Anthropic")
    def test_analyze_with_attachments_falls_back_when_unsupported(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = [
            RuntimeError("unsupported document input"),
            _make_anthropic_response("Claude fallback result"),
            _make_anthropic_response("Claude fallback result second"),
        ]

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}]

            provider = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
            first_result = provider.analyze_with_attachments("system", "user", attachments=attachments)
            second_result = provider.analyze_with_attachments("system", "user", attachments=attachments)

        self.assertEqual(first_result, "Claude fallback result")
        self.assertEqual(second_result, "Claude fallback result second")
        self.assertEqual(mock_client.messages.create.call_count, 3)
        fallback_prompt = mock_client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("File attachments were unavailable", fallback_prompt)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", fallback_prompt)
        self.assertIn("ts,name", fallback_prompt)

    @patch("anthropic.Anthropic")
    def test_analyze_retries_with_stream_for_long_requests(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = ValueError(
            "Streaming is required for operations that may take longer than 10 minutes. "
            "See https://github.com/anthropics/anthropic-sdk-python#long-requests for more details"
        )
        stream_obj = MagicMock()
        stream_obj.get_final_message.return_value = _make_anthropic_response("Claude streamed result")
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = stream_obj
        stream_ctx.__exit__.return_value = None
        mock_client.messages.stream.return_value = stream_ctx

        provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
        result = provider.analyze("system", "user", max_tokens=256000)

        self.assertEqual(result, "Claude streamed result")
        self.assertEqual(mock_client.messages.create.call_count, 1)
        self.assertEqual(mock_client.messages.stream.call_count, 1)
        stream_kwargs = mock_client.messages.stream.call_args.kwargs
        self.assertEqual(stream_kwargs["max_tokens"], 256000)
        self.assertEqual(stream_kwargs["messages"][0]["content"], "user")

    @patch("anthropic.Anthropic")
    def test_analyze_with_attachments_retries_with_stream_for_long_requests(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = ValueError(
            "Streaming is required for operations that may take longer than 10 minutes. "
            "See https://github.com/anthropics/anthropic-sdk-python#long-requests for more details"
        )
        stream_obj = MagicMock()
        stream_obj.get_final_message.return_value = _make_anthropic_response("Claude attachment streamed result")
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = stream_obj
        stream_ctx.__exit__.return_value = None
        mock_client.messages.stream.return_value = stream_ctx

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}],
                max_tokens=256000,
            )

        self.assertEqual(result, "Claude attachment streamed result")
        self.assertEqual(mock_client.messages.create.call_count, 1)
        self.assertEqual(mock_client.messages.stream.call_count, 1)
        stream_kwargs = mock_client.messages.stream.call_args.kwargs
        content_blocks = stream_kwargs["messages"][0]["content"]
        self.assertIsInstance(content_blocks, list)
        self.assertEqual(content_blocks[0]["type"], "text")
        self.assertEqual(content_blocks[1]["type"], "text")
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", content_blocks[1]["text"])

    @patch("anthropic.Anthropic")
    def test_analyze_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        class _FakeBadRequestError(Exception):
            pass

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _FakeBadRequestError(
                "maxtokens: 256000 > 128000, which is the maximum allowed number of output tokens for claude-opus-4-8"
            ),
            _make_anthropic_response("Claude capped result"),
        ]

        with patch("anthropic.BadRequestError", _FakeBadRequestError):
            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            result = provider.analyze("system", "user", max_tokens=256000)

        self.assertEqual(result, "Claude capped result")
        self.assertEqual(mock_client.messages.create.call_count, 2)
        first_kwargs = mock_client.messages.create.call_args_list[0].kwargs
        second_kwargs = mock_client.messages.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)

    @patch("anthropic.Anthropic")
    def test_token_limit_retry_delegates_to_shared_helper(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        """Pin that Claude's token-limit retry uses the shared base helper.

        The retry contract (catch ``BadRequestError``, extract the
        provider-declared cap, retry once with the reduced ``max_tokens``)
        must come from ``_run_with_completion_token_retry`` rather than a
        provider-local reimplementation that could drift.
        """
        mock_anthropic_cls.return_value = MagicMock()
        provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")

        create_fn = MagicMock(return_value="shared helper result")
        request_kwargs = {"model": "claude-opus-4-8", "max_tokens": 1024}
        with patch(
            "app.ai_providers.claude_provider._run_with_completion_token_retry",
            return_value="shared helper result",
        ) as mock_shared_retry:
            result = provider._with_token_limit_retry(create_fn, request_kwargs)

        self.assertEqual(result, "shared helper result")
        mock_shared_retry.assert_called_once_with(
            create_fn=create_fn,
            request_kwargs=request_kwargs,
            token_parameter="max_tokens",
            bad_request_error_type=provider._anthropic.BadRequestError,
            provider_name="Claude",
        )

    @patch("anthropic.Anthropic")
    def test_analyze_stream_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_anthropic_cls: MagicMock,
    ) -> None:
        class _FakeBadRequestError(Exception):
            pass

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = ValueError(
            "Streaming is required for operations that may take longer than 10 minutes. "
            "See https://github.com/anthropics/anthropic-sdk-python#long-requests for more details"
        )

        stream_obj = MagicMock()
        stream_obj.get_final_message.return_value = _make_anthropic_response("Claude streamed capped result")
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = stream_obj
        stream_ctx.__exit__.return_value = None
        mock_client.messages.stream.side_effect = [
            _FakeBadRequestError(
                "maxtokens: 256000 > 128000, which is the maximum allowed number of output tokens for claude-opus-4-8"
            ),
            stream_ctx,
        ]

        with patch("anthropic.BadRequestError", _FakeBadRequestError):
            provider = ClaudeProvider(api_key="sk-test", model="claude-opus-4-8")
            result = provider.analyze("system", "user", max_tokens=256000)

        self.assertEqual(result, "Claude streamed capped result")
        self.assertEqual(mock_client.messages.create.call_count, 1)
        self.assertEqual(mock_client.messages.stream.call_count, 2)
        first_stream_kwargs = mock_client.messages.stream.call_args_list[0].kwargs
        second_stream_kwargs = mock_client.messages.stream.call_args_list[1].kwargs
        self.assertEqual(first_stream_kwargs["max_tokens"], 256000)
        self.assertEqual(second_stream_kwargs["max_tokens"], 128000)

    @patch("anthropic.Anthropic")
    def test_analyze_connection_error(self, mock_anthropic_cls: MagicMock) -> None:
        class _FakeAPIConnectionError(Exception):
            pass

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = _FakeAPIConnectionError("connection failed")

        with patch("anthropic.APIConnectionError", _FakeAPIConnectionError):
            provider = ClaudeProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("Unable to connect", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_analyze_auth_error(self, mock_anthropic_cls: MagicMock) -> None:
        class _FakeAuthError(Exception):
            pass

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = _FakeAuthError("invalid key")

        with patch("anthropic.AuthenticationError", _FakeAuthError):
            provider = ClaudeProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("authentication failed", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_analyze_bad_request_context_length(self, mock_anthropic_cls: MagicMock) -> None:
        class _FakeBadRequestError(Exception):
            pass

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = _FakeBadRequestError("context_length_exceeded")

        with patch("anthropic.BadRequestError", _FakeBadRequestError):
            provider = ClaudeProvider(api_key="sk-test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("context length", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_analyze_with_pdf_attachment(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response("PDF result")

        with TemporaryDirectory(prefix="aift-test-") as tmp:
            pdf_path = Path(tmp) / "doc.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")

            provider = ClaudeProvider(api_key="sk-test")
            result = provider.analyze_with_attachments(
                "system", "user",
                attachments=[{"path": str(pdf_path), "name": "doc.pdf", "mime_type": "application/pdf"}],
            )

        self.assertEqual(result, "PDF result")
        content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "document")
        self.assertEqual(content[1]["source"]["media_type"], "application/pdf")



if __name__ == "__main__":
    unittest.main()
