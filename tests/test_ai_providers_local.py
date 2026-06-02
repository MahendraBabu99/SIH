"""Tests for the Local AI provider implementation and provider factory.

Covers TestLocalProvider and its helper classes (stream chunks, progress,
finalize, chat prompt), TestCreateProvider factory, TestAIProviderErrorPassthrough,
TestUploadAndRequestViaResponsesAPI, and TestAttachmentFallbackRegression.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from app.ai_providers import (
    AIProvider,
    AIProviderError,
    ClaudeProvider,
    KimiProvider,
    LocalProvider,
    OpenAIProvider,
    create_provider,
)
from app.ai_providers.base import (
    DEFAULT_CLOUD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_TOKENS,
    RATE_LIMIT_MAX_RETRIES,
    _RATE_LIMIT_STATE,
)
from app.ai_providers.utils import (
    _extract_openai_text,
    _inline_attachment_data_into_prompt,
    upload_and_request_via_responses_api,
)


def _make_openai_response(text: str) -> SimpleNamespace:
    """Build a minimal OpenAI-style chat completion response."""
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _raise_after_chunks(chunks: list[SimpleNamespace], error: Exception):
    """Yield test stream chunks, then raise ``error``."""
    yield from chunks
    raise error


# ---------------------------------------------------------------------------
# LocalProvider
# ---------------------------------------------------------------------------

class TestLocalProvider(unittest.TestCase):
    """Grouped tests for TestLocalProvider behavior."""

    @patch("openai.OpenAI")
    def test_analyze_returns_text(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "Local result"
        )

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        result = provider.analyze("system", "user")
        self.assertEqual(result, "Local result")

    @patch("openai.OpenAI")
    def test_analyze_stream_yields_text_chunks(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Local chunk 1 "))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Local chunk 2"))]),
        ]

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        chunks = list(provider.analyze_stream("system", "user"))

        self.assertEqual(chunks, ["Local chunk 1 ", "Local chunk 2"])
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])

    @patch("openai.OpenAI")
    def test_analyze_stream_empty_response_raises(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
        ]

        provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
        with self.assertRaises(AIProviderError) as ctx:
            list(provider.analyze_stream("system", "user"))
        self.assertIn("empty", str(ctx.exception).lower())

    @patch("openai.OpenAI")
    def test_analyze_stream_refusal_raises(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(refusal="Refused"))]),
        ]

        provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
        with self.assertRaises(AIProviderError) as ctx:
            list(provider.analyze_stream("system", "user"))
        self.assertIn("refused", str(ctx.exception))
        self.assertIn("Refused", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_stream_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
            def __init__(self, message: str, *, param: str | None = None) -> None:
                """Initialize the test helper."""
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
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Local capped stream"))]
                )
            ],
        ]

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
            chunks = list(provider.analyze_stream("system", "user", max_tokens=256000))

        self.assertEqual(chunks, ["Local capped stream"])
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)
        self.assertTrue(second_kwargs["stream"])

    @patch("openai.OpenAI")
    def test_get_model_info(self, _mock: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        info = provider.get_model_info()
        self.assertEqual(info["provider"], "local")
        self.assertEqual(info["model"], "llama3.1:70b")

    @patch("openai.OpenAI")
    def test_default_api_key(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        self.assertEqual(provider._api_key, "not-needed")

    @patch("openai.OpenAI")
    def test_normalizes_root_base_url_to_v1(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        LocalProvider(base_url="http://localhost:11434/", model="llama3.1:70b")
        kwargs = mock_openai_cls.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "http://localhost:11434/v1")

    @patch("openai.OpenAI")
    def test_uses_configured_timeout_and_disables_internal_retries(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        LocalProvider(
            base_url="http://localhost:11434/v1",
            model="llama3.1:70b",
            request_timeout_seconds=7200,
        )
        kwargs = mock_openai_cls.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 7200.0)
        self.assertEqual(kwargs["max_retries"], 0)

    @patch("openai.OpenAI")
    def test_timeout_errors_surface_timeout_guidance(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        class _FakeAPIConnectionError(Exception):
            """Grouped tests for _FakeAPIConnectionError behavior."""
            pass

        class _FakeAPITimeoutError(_FakeAPIConnectionError):
            """Grouped tests for _FakeAPITimeoutError behavior."""
            pass

        with patch("openai.APIConnectionError", _FakeAPIConnectionError), patch(
            "openai.APITimeoutError",
            _FakeAPITimeoutError,
        ):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="llama3.1:70b",
                request_timeout_seconds=1800,
            )
            mock_client.chat.completions.create.side_effect = _FakeAPITimeoutError(
                "request timed out"
            )

            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")

        self.assertIn("timed out after 1800 seconds", str(ctx.exception))
        self.assertIn("ai.local.request_timeout_seconds", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_connection_error_without_timeout(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        class _FakeAPIConnectionError(Exception):
            """Grouped tests for _FakeAPIConnectionError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        with patch("openai.APIConnectionError", _FakeAPIConnectionError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="llama3.1:70b",
            )
            mock_client.chat.completions.create.side_effect = _FakeAPIConnectionError(
                "connection refused"
            )
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("Unable to connect", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_auth_error(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        class _FakeAuthError(Exception):
            """Grouped tests for _FakeAuthError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        with patch("openai.AuthenticationError", _FakeAuthError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
            )
            mock_client.chat.completions.create.side_effect = _FakeAuthError("bad key")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("rejected authentication", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_api_error_404(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        class _FakeAPIError(Exception):
            """Grouped tests for _FakeAPIError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        with patch("openai.APIError", _FakeAPIError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
            )
            mock_client.chat.completions.create.side_effect = _FakeAPIError("404 not found")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("404", str(ctx.exception))
            self.assertIn("base URL", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_api_error_generic(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        class _FakeAPIError(Exception):
            """Grouped tests for _FakeAPIError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        with patch("openai.APIError", _FakeAPIError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
            )
            mock_client.chat.completions.create.side_effect = _FakeAPIError("internal server error")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("Local provider API error", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_context_length_error(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
            )
            mock_client.chat.completions.create.side_effect = _FakeBadRequestError(
                "context_length_exceeded"
            )
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze("system", "user")
            self.assertIn("context length", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_with_progress_streams_thinking_and_returns_final_text(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        chunk1 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(reasoning="Thinking step 1. "),
                )
            ]
        )
        chunk2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="Final answer."),
                )
            ]
        )
        mock_client.chat.completions.create.return_value = [chunk1, chunk2]

        progress_updates: list[dict[str, str]] = []
        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda payload: progress_updates.append(payload),
        )

        self.assertEqual(result, "Final answer.")
        self.assertTrue(progress_updates)
        self.assertEqual(progress_updates[-1]["status"], "thinking")
        self.assertIn("Thinking step 1.", progress_updates[-1]["thinking_text"])

    @patch("openai.OpenAI")
    def test_analyze_with_progress_removes_streamed_reasoning_prefix_from_final_answer(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        reasoning_text = "I will reason through all artifact records before answering. "
        chunk1 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(reasoning=reasoning_text),
                )
            ]
        )
        chunk2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=reasoning_text),
                )
            ]
        )
        chunk3 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="### Findings\n- Suspicious autorun entry."),
                )
            ]
        )
        mock_client.chat.completions.create.return_value = [chunk1, chunk2, chunk3]

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda _payload: None,
        )

        self.assertEqual(result, "### Findings\n- Suspicious autorun entry.")

    @patch("openai.OpenAI")
    def test_analyze_with_progress_strips_leading_think_block_from_final_answer(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="<think>\ninternal reasoning\n</think>\n\n### Findings\n- Final answer."
                    ),
                )
            ]
        )
        mock_client.chat.completions.create.return_value = [chunk]

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=lambda _payload: None,
        )

        self.assertEqual(result, "### Findings\n- Final answer.")
        self.assertNotIn("<think>", result)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_separates_streamed_think_markup(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Leading streamed think blocks stay out of partial and final answers."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="<think>hidden "))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="reasoning</think>\nVisible "))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="answer."))]),
        ]
        progress_updates: list[dict[str, str]] = []

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="test"
        )
        result = provider.analyze_with_progress(
            "system",
            "user",
            progress_callback=progress_updates.append,
        )

        self.assertEqual(result, "Visible answer.")
        self.assertTrue(
            any("hidden reasoning" in update.get("thinking_text", "") for update in progress_updates)
        )
        self.assertFalse(any("<think>" in update.get("partial_text", "") for update in progress_updates))
        self.assertFalse(
            any("hidden reasoning" in update.get("partial_text", "") for update in progress_updates)
        )

    @patch("openai.OpenAI")
    def test_analyze_with_progress_retries_with_model_token_cap_when_too_large(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
            def __init__(self, message: str, *, param: str | None = None) -> None:
                """Initialize the test helper."""
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
                    choices=[SimpleNamespace(delta=SimpleNamespace(reasoning="Thinking. "))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Final answer."))]
                ),
            ],
        ]

        progress_updates: list[dict[str, str]] = []
        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda payload: progress_updates.append(payload),
                max_tokens=256000,
            )

        self.assertEqual(result, "Final answer.")
        self.assertTrue(progress_updates)
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)
        self.assertTrue(second_kwargs["stream"])

    @patch("openai.OpenAI")
    def test_analyze_with_progress_with_attachments_falls_back_to_stream_with_inlined_prompt(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-unsupported")
        mock_client.responses.create.side_effect = RuntimeError("unrecognized request url /responses")
        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="Local streamed fallback result"),
                )
            ]
        )
        mock_client.chat.completions.create.return_value = [chunk]

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}]

            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda _payload: None,
                attachments=attachments,
            )

        self.assertEqual(result, "Local streamed fallback result")
        self.assertEqual(mock_client.files.create.call_count, 1)
        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)
        stream_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertTrue(stream_kwargs["stream"])
        stream_prompt = stream_kwargs["messages"][1]["content"]
        self.assertIn("File attachments were unavailable", stream_prompt)
        self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", stream_prompt)
        self.assertIn("ts,name", stream_prompt)

    @patch("openai.OpenAI")
    def test_analyze_strips_leading_think_block_in_non_stream_mode(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "<think>\nhidden chain-of-thought\n</think>\n\nFinal answer."
        )

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        result = provider.analyze("system", "user")

        self.assertEqual(result, "Final answer.")

    @patch("openai.OpenAI")
    def test_analyze_reasoning_only_think_block_raises_empty_response(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Reasoning-only local markup must not become final answer text."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "<think>hidden chain-of-thought</think>"
        )

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        with self.assertRaises(AIProviderError) as ctx:
            provider.analyze("system", "user")

        self.assertIn("empty response", str(ctx.exception))
        self.assertNotIn("hidden chain-of-thought", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_unterminated_leading_think_block_raises_empty_response(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Truncated local reasoning markup must not become final answer text."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            "<think>hidden chain-of-thought before truncation\nPotential answer"
        )

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="llama3.1:70b"
        )
        with self.assertRaises(AIProviderError) as ctx:
            provider.analyze("system", "user")

        self.assertIn("empty response", str(ctx.exception))
        self.assertNotIn("hidden chain-of-thought", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_retries_with_model_token_cap_when_max_tokens_too_large(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
            def __init__(self, message: str, *, param: str | None = None) -> None:
                """Initialize the test helper."""
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
            _make_openai_response("<think>hidden</think>\n\nLocal capped result"),
        ]

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            result = provider.analyze("system", "user", max_tokens=256000)

        self.assertEqual(result, "Local capped result")
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["max_tokens"], 256000)
        self.assertEqual(second_kwargs["max_tokens"], 128000)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_uses_responses_api_when_supported(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-123")
        mock_client.responses.create.return_value = SimpleNamespace(output_text="Attachment result")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}],
            )

        self.assertEqual(result, "Attachment result")
        self.assertEqual(mock_client.files.create.call_count, 1)
        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertEqual(mock_client.files.delete.call_count, 1)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_reasoning_only_response_raises_empty(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Responses API reasoning markup is not returned as attachment answer text."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-123")
        mock_client.responses.create.return_value = SimpleNamespace(
            output_text="<think>hidden attachment reasoning</think>"
        )

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze_with_attachments(
                    "system",
                    "user",
                    attachments=[
                        {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
                    ],
                )

        self.assertIn("empty response", str(ctx.exception))
        self.assertNotIn("hidden attachment reasoning", str(ctx.exception))
        self.assertEqual(mock_client.files.delete.call_count, 1)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_unterminated_reasoning_raises_empty(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Truncated Responses API reasoning markup is not attachment answer text."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-123")
        mock_client.responses.create.return_value = SimpleNamespace(
            output_text="<reasoning>hidden attachment reasoning before truncation\nPotential answer"
        )

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")

            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze_with_attachments(
                    "system",
                    "user",
                    attachments=[
                        {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
                    ],
                )

        self.assertIn("empty response", str(ctx.exception))
        self.assertNotIn("hidden attachment reasoning", str(ctx.exception))
        self.assertEqual(mock_client.files.delete.call_count, 1)

    @patch("openai.OpenAI")
    def test_analyze_with_attachments_falls_back_when_endpoint_unsupported(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.files.create.return_value = SimpleNamespace(id="file-unsupported")
        mock_client.responses.create.side_effect = RuntimeError("unrecognized request url /responses")
        mock_client.chat.completions.create.return_value = _make_openai_response("Fallback result")

        with TemporaryDirectory(prefix="aift-ai-provider-test-") as temp_dir:
            csv_path = Path(temp_dir) / "runkeys.csv"
            csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
            attachments = [{"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}]

            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="llama3.1:70b"
            )
            first_result = provider.analyze_with_attachments("system", "user", attachments=attachments)
            second_result = provider.analyze_with_attachments("system", "user", attachments=attachments)

        self.assertEqual(first_result, "Fallback result")
        self.assertEqual(second_result, "Fallback result")
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
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
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
                provider = LocalProvider(
                    base_url="http://localhost:11434/v1", model="llama3.1:70b"
                )
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
    def test_analyze_with_progress_no_callback_delegates_to_analyze_with_attachments(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response("result")

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="test"
        )
        result = provider.analyze_with_progress(
            "system", "user", progress_callback=None
        )
        self.assertEqual(result, "result")

    @patch("openai.OpenAI")
    def test_analyze_non_stream_empty_with_finish_reason(
        self, mock_openai_cls: MagicMock
    ) -> None:
        """Verify the behavior described by this test name."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        choice = SimpleNamespace(
            message=SimpleNamespace(content=""),
            finish_reason="length",
        )
        mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])

        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="test"
        )
        with self.assertRaises(AIProviderError) as ctx:
            provider.analyze("system", "user")
        self.assertIn("finish_reason=length", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_analyze_stream_falls_back_to_non_stream_when_unsupported(
        self, mock_openai_cls: MagicMock
    ) -> None:
        """Verify the behavior described by this test name."""
        class _FakeBadRequestError(Exception):
            """Grouped tests for _FakeBadRequestError behavior."""
            pass

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _FakeBadRequestError("stream is not supported by this endpoint"),
            _make_openai_response("Non-stream fallback"),
        ]

        with patch("openai.BadRequestError", _FakeBadRequestError):
            provider = LocalProvider(
                base_url="http://localhost:11434/v1", model="test"
            )
            chunks = list(provider.analyze_stream("system", "user"))

        self.assertEqual(chunks, ["Non-stream fallback"])

    @patch("openai.OpenAI")
    def test_analyze_with_progress_retries_rate_limit_before_output(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Progress streams retry rate limits only before visible output."""
        class _FakeRateLimitError(Exception):
            """Fake rate-limit error for progress retry tests."""

        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _FakeRateLimitError("rate limited before output"),
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Recovered"))]),
            ],
        ]

        with patch("openai.RateLimitError", _FakeRateLimitError), patch(
            "app.ai_providers.base.time.sleep"
        ) as mock_sleep:
            provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
            result = provider.analyze_with_progress(
                "system",
                "user",
                progress_callback=lambda _payload: None,
            )

        self.assertEqual(result, "Recovered")
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        self.assertTrue(mock_sleep.called)
        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_rate_limit_after_answer_does_not_retry(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Progress streams stop instead of replaying after answer output."""
        class _FakeRateLimitError(Exception):
            """Fake rate-limit error for progress retry tests."""

        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = _FakeRateLimitError("rate limited after answer")
        mock_client.chat.completions.create.return_value = _raise_after_chunks(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Partial "))]),
            ],
            error,
        )
        progress_updates: list[dict[str, str]] = []

        with patch("openai.RateLimitError", _FakeRateLimitError), patch(
            "app.ai_providers.base.time.sleep"
        ) as mock_sleep:
            provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
            with self.assertRaises(AIProviderError) as ctx:
                provider.analyze_with_progress(
                    "system",
                    "user",
                    progress_callback=progress_updates.append,
                )

        self.assertIn("partial output", str(ctx.exception))
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)
        self.assertFalse(mock_sleep.called)
        self.assertTrue(any("Partial" in item.get("partial_text", "") for item in progress_updates))
        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)

    @patch("openai.OpenAI")
    def test_analyze_with_progress_rate_limit_after_reasoning_does_not_retry(
        self,
        mock_openai_cls: MagicMock,
    ) -> None:
        """Reasoning progress also blocks automatic stream replay."""
        class _FakeRateLimitError(Exception):
            """Fake rate-limit error for progress retry tests."""

        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = _FakeRateLimitError("rate limited after reasoning")
        mock_client.chat.completions.create.return_value = _raise_after_chunks(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(reasoning="Thinking before failure"))]
                ),
            ],
            error,
        )
        progress_updates: list[dict[str, str]] = []

        with patch("openai.RateLimitError", _FakeRateLimitError), patch(
            "app.ai_providers.base.time.sleep"
        ) as mock_sleep:
            provider = LocalProvider(base_url="http://localhost:11434/v1", model="test")
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
            any("Thinking before failure" in item.get("thinking_text", "") for item in progress_updates)
        )
        _RATE_LIMIT_STATE.pop("Local/OpenAI-compatible", None)


# ---------------------------------------------------------------------------
# LocalProvider._process_stream_chunk
# ---------------------------------------------------------------------------

class TestLocalProviderProcessStreamChunk(unittest.TestCase):
    """Grouped tests for TestLocalProviderProcessStreamChunk behavior."""
    def test_returns_none_for_no_choices(self) -> None:
        """Verify the behavior described by this test name."""
        chunk = SimpleNamespace(choices=[])
        self.assertIsNone(LocalProvider._process_stream_chunk(chunk))

    def test_returns_none_for_none_delta(self) -> None:
        """Verify the behavior described by this test name."""
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=None)])
        self.assertIsNone(LocalProvider._process_stream_chunk(chunk))

    def test_extracts_thinking_and_answer(self) -> None:
        """Verify the behavior described by this test name."""
        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="answer",
                        reasoning="thinking",
                    ),
                )
            ]
        )
        result = LocalProvider._process_stream_chunk(chunk)
        self.assertIsNotNone(result)
        thinking, answer = result
        self.assertEqual(thinking, "thinking")
        self.assertEqual(answer, "answer")

    def test_returns_none_for_empty_deltas(self) -> None:
        """Verify the behavior described by this test name."""
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace())]
        )
        self.assertIsNone(LocalProvider._process_stream_chunk(chunk))

    def test_handles_dict_choice(self) -> None:
        """Verify the behavior described by this test name."""
        chunk = SimpleNamespace(choices=[{"delta": {"content": "from dict"}}])
        result = LocalProvider._process_stream_chunk(chunk)
        self.assertIsNotNone(result)
        thinking, answer = result
        self.assertEqual(answer, "from dict")
        self.assertEqual(thinking, "")


# ---------------------------------------------------------------------------
# LocalProvider._emit_progress_if_needed
# ---------------------------------------------------------------------------

class TestLocalProviderEmitProgressIfNeeded(unittest.TestCase):
    """Grouped tests for TestLocalProviderEmitProgressIfNeeded behavior."""
    def test_no_emit_when_no_content(self) -> None:
        """Verify the behavior described by this test name."""
        callback = MagicMock()
        result = LocalProvider._emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="",
            current_answer="",
            last_emit_at=0.0,
            last_sent_thinking="",
            last_sent_answer="",
        )
        callback.assert_not_called()
        self.assertEqual(result[0], 0.0)

    def test_no_emit_when_unchanged(self) -> None:
        """Verify the behavior described by this test name."""
        callback = MagicMock()
        result = LocalProvider._emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="same",
            current_answer="same",
            last_emit_at=0.0,
            last_sent_thinking="same",
            last_sent_answer="same",
        )
        callback.assert_not_called()

    def test_emits_when_enough_change(self) -> None:
        """Verify the behavior described by this test name."""
        callback = MagicMock()
        long_text = "x" * 100
        result = LocalProvider._emit_progress_if_needed(
            progress_callback=callback,
            current_thinking=long_text,
            current_answer="",
            last_emit_at=0.0,
            last_sent_thinking="",
            last_sent_answer="",
        )
        callback.assert_called_once()
        self.assertGreater(result[0], 0.0)
        self.assertEqual(result[1], long_text)

    def test_rate_limits_small_changes(self) -> None:
        """Verify the behavior described by this test name."""
        callback = MagicMock()
        now = time.monotonic()
        result = LocalProvider._emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="a",
            current_answer="",
            last_emit_at=now,
            last_sent_thinking="",
            last_sent_answer="",
        )
        callback.assert_not_called()
        self.assertEqual(result[0], now)

    def test_handles_callback_exception(self) -> None:
        """Verify the behavior described by this test name."""
        def bad_callback(payload):
            """Support test behavior for bad_callback."""
            raise RuntimeError("callback failed")

        long_text = "x" * 100
        result = LocalProvider._emit_progress_if_needed(
            progress_callback=bad_callback,
            current_thinking=long_text,
            current_answer="",
            last_emit_at=0.0,
            last_sent_thinking="",
            last_sent_answer="",
        )
        self.assertGreater(result[0], 0.0)


# ---------------------------------------------------------------------------
# LocalProvider._finalize_stream_response
# ---------------------------------------------------------------------------

class TestLocalProviderFinalizeStreamResponse(unittest.TestCase):
    """Grouped tests for TestLocalProviderFinalizeStreamResponse behavior."""
    def test_returns_answer_when_present(self) -> None:
        """Verify the behavior described by this test name."""
        result = LocalProvider._finalize_stream_response(
            thinking_parts=["thinking"],
            answer_parts=["answer"],
        )
        self.assertEqual(result, "answer")

    def test_raises_when_only_thinking_is_present(self) -> None:
        """Reasoning-only streams must not become final answer text."""
        with self.assertRaises(AIProviderError):
            LocalProvider._finalize_stream_response(
                thinking_parts=["thinking only"],
                answer_parts=[],
            )

    def test_raises_when_both_empty(self) -> None:
        """Verify the behavior described by this test name."""
        with self.assertRaises(AIProviderError):
            LocalProvider._finalize_stream_response(
                thinking_parts=[],
                answer_parts=[],
            )

    def test_strips_think_block_from_answer(self) -> None:
        """Verify the behavior described by this test name."""
        result = LocalProvider._finalize_stream_response(
            thinking_parts=[],
            answer_parts=["<think>reasoning</think>\nFinal."],
        )
        self.assertEqual(result, "Final.")


# ---------------------------------------------------------------------------
# LocalProvider._build_chat_completion_prompt
# ---------------------------------------------------------------------------

class TestLocalProviderBuildChatCompletionPrompt(unittest.TestCase):
    """Grouped tests for TestLocalProviderBuildChatCompletionPrompt behavior."""

    @patch("openai.OpenAI")
    def test_returns_user_prompt_without_attachments(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        provider = LocalProvider(
            base_url="http://localhost:11434/v1", model="test"
        )
        result = provider._build_chat_completion_prompt("user prompt", None)
        self.assertEqual(result, "user prompt")

    @patch("openai.OpenAI")
    def test_inlines_attachments_when_available(self, mock_openai_cls: MagicMock) -> None:
        """Verify the behavior described by this test name."""
        with TemporaryDirectory(prefix="aift-test-") as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("a,b\n1,2\n")

            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
                attach_csv_as_file=True,
            )
            result = provider._build_chat_completion_prompt(
                "analyze",
                [{"path": str(path), "name": "data.csv", "mime_type": "text/csv"}],
            )
            self.assertIn("--- BEGIN ATTACHMENT: data.csv ---", result)

    @patch("openai.OpenAI")
    def test_inlines_attachments_even_when_attach_flag_disabled(self, mock_openai_cls: MagicMock) -> None:
        """When attach_csv_as_file=False, attachments must still be inlined."""
        with TemporaryDirectory(prefix="aift-test-") as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("a,b\n1,2\n")

            provider = LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
                attach_csv_as_file=False,
            )
            result = provider._build_chat_completion_prompt(
                "prompt",
                [{"path": str(path), "name": "data.csv", "mime_type": "text/csv"}],
            )
            self.assertIn("--- BEGIN ATTACHMENT: data.csv ---", result)
            self.assertIn("a,b", result)


# ---------------------------------------------------------------------------
# create_provider factory
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
