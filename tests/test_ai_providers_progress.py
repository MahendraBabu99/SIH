"""Tests for the shared AI provider streaming-progress plumbing.

Covers the best-effort progress emitter, the throttled emitter, the shared
progress-chunk generator, the final streamed-response validation, and the
consolidation guarantees that keep Claude, Kimi, and Local providers on the
same implementation.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from app.ai_providers.base import AIProviderError
from app.ai_providers.local_provider import LocalProvider
from app.ai_providers.progress import (
    emit_progress,
    emit_progress_if_needed,
    finalize_progress_stream_response,
    stream_progress_chunks,
)
from app.ai_providers.utils import StreamedResponseChunk


class TestEmitProgress(unittest.TestCase):
    """Grouped tests for the best-effort progress emitter."""

    def test_invokes_callback_with_thinking_payload(self) -> None:
        """The callback receives status plus both text channels."""
        callback = MagicMock()
        emit_progress(callback, thinking_text="thought", partial_text="answer")
        callback.assert_called_once_with(
            {
                "status": "thinking",
                "thinking_text": "thought",
                "partial_text": "answer",
            }
        )

    def test_swallows_callback_exception(self) -> None:
        """A broken callback must not abort the caller."""
        def bad_callback(payload: dict[str, str]) -> None:
            """Support test behavior for bad_callback."""
            raise RuntimeError("callback failed")

        emit_progress(bad_callback, thinking_text="t", partial_text="a")


class TestEmitProgressIfNeeded(unittest.TestCase):
    """Grouped tests for the shared throttled progress emitter."""

    def test_no_emit_when_no_content(self) -> None:
        """Empty channels never trigger an emission."""
        callback = MagicMock()
        result = emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="",
            current_answer="",
            last_emit_at=0.0,
            last_sent_thinking="",
            last_sent_answer="",
        )
        callback.assert_not_called()
        self.assertEqual(result, (0.0, "", ""))

    def test_no_emit_when_unchanged(self) -> None:
        """Unchanged channel text never triggers an emission."""
        callback = MagicMock()
        emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="same",
            current_answer="same",
            last_emit_at=0.0,
            last_sent_thinking="same",
            last_sent_answer="same",
        )
        callback.assert_not_called()

    def test_emits_when_enough_change(self) -> None:
        """Large channel growth bypasses the time throttle."""
        callback = MagicMock()
        long_text = "x" * 100
        result = emit_progress_if_needed(
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
        """Small recent changes are deferred by the throttle."""
        callback = MagicMock()
        now = time.monotonic()
        result = emit_progress_if_needed(
            progress_callback=callback,
            current_thinking="a",
            current_answer="",
            last_emit_at=now,
            last_sent_thinking="",
            last_sent_answer="",
        )
        callback.assert_not_called()
        self.assertEqual(result[0], now)


class TestStreamProgressChunks(unittest.TestCase):
    """Grouped tests for the shared progress-chunk generator."""

    def test_accumulates_channels_and_yields_chunks_unchanged(self) -> None:
        """Chunks pass through while channel parts accumulate."""
        callback = MagicMock()
        chunks = [
            StreamedResponseChunk(reasoning_text="thinking..."),
            StreamedResponseChunk(answer_text="Hello "),
            StreamedResponseChunk(answer_text="world."),
        ]
        thinking_parts: list[str] = []
        answer_parts: list[str] = []

        yielded = list(
            stream_progress_chunks(
                chunks=iter(chunks),
                progress_callback=callback,
                thinking_parts=thinking_parts,
                answer_parts=answer_parts,
            )
        )

        self.assertEqual(yielded, chunks)
        self.assertEqual(thinking_parts, ["thinking..."])
        self.assertEqual(answer_parts, ["Hello ", "world."])

    def test_emits_final_progress_update(self) -> None:
        """A final emission carries the complete accumulated text."""
        callback = MagicMock()
        list(
            stream_progress_chunks(
                chunks=iter([StreamedResponseChunk(answer_text="Final answer.")]),
                progress_callback=callback,
                thinking_parts=[],
                answer_parts=[],
            )
        )
        self.assertGreaterEqual(callback.call_count, 1)
        final_payload = callback.call_args[0][0]
        self.assertEqual(final_payload["status"], "thinking")
        self.assertEqual(final_payload["partial_text"], "Final answer.")

    def test_no_final_emit_for_empty_stream(self) -> None:
        """A stream with no text never invokes the callback."""
        callback = MagicMock()
        list(
            stream_progress_chunks(
                chunks=iter([]),
                progress_callback=callback,
                thinking_parts=[],
                answer_parts=[],
            )
        )
        callback.assert_not_called()


class TestFinalizeProgressStreamResponse(unittest.TestCase):
    """Grouped tests for the shared final streamed-response validation."""

    def test_returns_answer_when_present(self) -> None:
        """Answer-channel text becomes the final response."""
        result = finalize_progress_stream_response(
            ["thinking"],
            ["answer"],
            empty_response_message="empty",
        )
        self.assertEqual(result, "answer")

    def test_raises_provider_message_when_only_thinking(self) -> None:
        """Reasoning-only streams raise the provider-specific message."""
        with self.assertRaises(AIProviderError) as ctx:
            finalize_progress_stream_response(
                ["thinking only"],
                [],
                empty_response_message="Provider X returned nothing.",
            )
        self.assertEqual(str(ctx.exception), "Provider X returned nothing.")

    def test_strips_think_block_from_answer(self) -> None:
        """Leading reasoning markup is removed from the final answer."""
        result = finalize_progress_stream_response(
            [],
            ["<think>reasoning</think>\nFinal."],
            empty_response_message="empty",
        )
        self.assertEqual(result, "Final.")


class TestProviderConsolidation(unittest.TestCase):
    """Pin that providers share the single progress implementation."""

    def test_local_provider_alias_is_shared_function(self) -> None:
        """``LocalProvider._emit_progress_if_needed`` is the shared helper."""
        self.assertIs(LocalProvider._emit_progress_if_needed, emit_progress_if_needed)


if __name__ == "__main__":
    unittest.main()
