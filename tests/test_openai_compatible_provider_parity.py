"""Parity tests for OpenAI-compatible provider shared behavior."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.ai_providers import (
    AIProvider,
    AIProviderError,
    KimiProvider,
    LocalProvider,
    OpenAIProvider,
    stream_chunk_answer_text,
    stream_chunk_reasoning_text,
)


def _make_openai_response(text: str) -> SimpleNamespace:
    """Build a minimal OpenAI-style chat completion response."""
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _provider_factories() -> list[tuple[str, Callable[[], AIProvider]]]:
    """Return provider constructors that exercise the shared mixin."""
    return [
        ("OpenAI", lambda: OpenAIProvider(api_key="sk-test", model="gpt-4o")),
        ("Kimi", lambda: KimiProvider(api_key="sk-test", model="kimi-k2.6")),
        (
            "Local",
            lambda: LocalProvider(base_url="http://localhost:11434/v1", model="test-model"),
        ),
    ]


class TestOpenAICompatibleProviderParity(unittest.TestCase):
    """Shared behavior should stay aligned across compatible providers."""

    def test_streaming_splits_answer_and_reasoning_channels(self) -> None:
        """Reasoning deltas stay separate while answer text remains string-compatible."""
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.chat.completions.create.return_value = [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    reasoning="Reasoning detail. ",
                                    content="Visible answer.",
                                )
                            )
                        ]
                    )
                ]

                chunks = list(provider_factory().analyze_stream("system", "user"))

                self.assertEqual([stream_chunk_answer_text(chunk) for chunk in chunks], ["Visible answer."])
                self.assertEqual([stream_chunk_reasoning_text(chunk) for chunk in chunks], ["Reasoning detail. "])

    def test_attachment_unsupported_fallback_is_cached_for_text_mode(self) -> None:
        """Unsupported file APIs fall back to inline text and skip repeat uploads."""
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.files.create.return_value = SimpleNamespace(id="file-unsupported")
                mock_client.responses.create.side_effect = RuntimeError(
                    "unrecognized request url /responses"
                )
                mock_client.chat.completions.create.return_value = _make_openai_response(
                    "Fallback result"
                )

                with TemporaryDirectory(prefix="aift-provider-parity-") as tmp_dir:
                    csv_path = Path(tmp_dir) / "runkeys.csv"
                    csv_path.write_text("ts,name\n2026-01-15T12:00:00Z,EntryA\n", encoding="utf-8")
                    attachments = [
                        {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
                    ]

                    provider = provider_factory()
                    first_result = provider.analyze_with_attachments(
                        "system",
                        "user",
                        attachments=attachments,
                    )
                    second_result = provider.analyze_with_attachments(
                        "system",
                        "user",
                        attachments=attachments,
                    )

                self.assertEqual(first_result, "Fallback result")
                self.assertEqual(second_result, "Fallback result")
                self.assertEqual(mock_client.files.create.call_count, 1)
                self.assertEqual(mock_client.responses.create.call_count, 1)
                self.assertEqual(mock_client.chat.completions.create.call_count, 2)
                first_prompt = mock_client.chat.completions.create.call_args_list[0].kwargs[
                    "messages"
                ][1]["content"]
                second_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs[
                    "messages"
                ][1]["content"]
                self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", first_prompt)
                self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", second_prompt)

    def test_bare_attachment_404_falls_back_to_inline_text(self) -> None:
        """Bare 404s from attachment routes are treated as unsupported file APIs."""
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.files.create.return_value = SimpleNamespace(id="file-404")
                mock_client.responses.create.side_effect = RuntimeError("404 Not Found")
                mock_client.chat.completions.create.return_value = _make_openai_response(
                    "Inline fallback result"
                )

                with TemporaryDirectory(prefix="aift-provider-parity-") as tmp_dir:
                    csv_path = Path(tmp_dir) / "runkeys.csv"
                    csv_path.write_text(
                        "ts,name\n2026-01-15T12:00:00Z,EntryA\n",
                        encoding="utf-8",
                    )
                    attachments = [
                        {"path": str(csv_path), "name": "runkeys.csv", "mime_type": "text/csv"}
                    ]

                    provider = provider_factory()
                    result = provider.analyze_with_attachments(
                        "system",
                        "user",
                        attachments=attachments,
                    )

                self.assertEqual(result, "Inline fallback result")
                self.assertEqual(mock_client.files.create.call_count, 1)
                self.assertEqual(mock_client.responses.create.call_count, 1)
                self.assertEqual(mock_client.chat.completions.create.call_count, 1)
                fallback_prompt = mock_client.chat.completions.create.call_args.kwargs[
                    "messages"
                ][1]["content"]
                self.assertIn("File attachments were unavailable", fallback_prompt)
                self.assertIn("--- BEGIN ATTACHMENT: runkeys.csv ---", fallback_prompt)
                self.assertIn("ts,name", fallback_prompt)

    def test_progress_mode_streams_answer_and_progress_updates(self) -> None:
        """Progress mode returns the answer and forwards both text channels."""
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.chat.completions.create.return_value = [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    reasoning="Reasoning detail. ",
                                    content="Visible answer.",
                                )
                            )
                        ]
                    )
                ]
                progress_updates: list[dict[str, str]] = []

                result = provider_factory().analyze_with_progress(
                    "system",
                    "user",
                    progress_callback=progress_updates.append,
                )

                self.assertEqual(result, "Visible answer.")
                self.assertTrue(progress_updates)
                final_update = progress_updates[-1]
                self.assertEqual(final_update["status"], "thinking")
                self.assertEqual(final_update["thinking_text"], "Reasoning detail.")
                self.assertEqual(final_update["partial_text"], "Visible answer.")

    def test_progress_mode_empty_stream_messages_stay_provider_specific(self) -> None:
        """Reasoning-only progress streams raise each provider's exact message."""
        expected_messages = {
            "OpenAI": (
                "OpenAI returned an empty streamed response. "
                "Try increasing max tokens."
            ),
            "Kimi": (
                "Kimi returned an empty streamed response. "
                "Try increasing max tokens."
            ),
            "Local": (
                "Local AI provider returned an empty streamed response. "
                "Try a different local model or increase max tokens."
            ),
        }
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.chat.completions.create.return_value = [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    reasoning="Reasoning only. ",
                                    content=None,
                                )
                            )
                        ]
                    )
                ]

                with self.assertRaises(AIProviderError) as ctx:
                    provider_factory().analyze_with_progress(
                        "system",
                        "user",
                        progress_callback=lambda _payload: None,
                    )

                self.assertEqual(str(ctx.exception), expected_messages[provider_name])

    def test_progress_mode_empty_final_messages_stay_provider_specific(self) -> None:
        """Answers that clean to nothing raise each provider's exact message."""
        expected_messages = {
            "OpenAI": (
                "OpenAI returned an empty streamed response. "
                "This can happen with reasoning-only outputs or very low token limits."
            ),
            "Kimi": (
                "Kimi returned an empty streamed response. "
                "This can happen with reasoning-only outputs or very low token limits."
            ),
            "Local": (
                "Local AI provider returned an empty streamed response. "
                "Try a different local model or increase max tokens."
            ),
        }
        for provider_name, provider_factory in _provider_factories():
            with self.subTest(provider=provider_name), patch("openai.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client
                mock_client.chat.completions.create.return_value = [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="<think>only reasoning markup</think>",
                                )
                            )
                        ]
                    )
                ]

                with self.assertRaises(AIProviderError) as ctx:
                    provider_factory().analyze_with_progress(
                        "system",
                        "user",
                        progress_callback=lambda _payload: None,
                    )

                self.assertEqual(str(ctx.exception), expected_messages[provider_name])


if __name__ == "__main__":
    unittest.main()
