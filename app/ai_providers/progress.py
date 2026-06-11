"""Shared streaming-progress plumbing for AI providers.

Claude, Kimi, and Local providers all support a "progress mode" where the
model response is streamed and separated thinking/answer text is forwarded
to a GUI progress callback. The throttling, channel accumulation, and
final-response validation logic is identical across providers, so this
module is the single shared implementation. Providers keep only the
provider-specific pieces: how a raw SDK stream is split into chunks and
which user-facing message describes an empty streamed response.

Attributes:
    PROGRESS_EMIT_MIN_INTERVAL_SECONDS: Minimum delay between progress
        callback emissions while neither channel grew substantially.
    PROGRESS_EMIT_MIN_NEW_CHARS: Character growth on either channel that
        forces an emission regardless of the time throttle.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Iterator

from .base import AIProviderError
from .utils import (
    _clean_streamed_answer_text,
    stream_chunk_answer_text,
    stream_chunk_reasoning_text,
)

PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.35
PROGRESS_EMIT_MIN_NEW_CHARS = 80


def emit_progress(
    progress_callback: Callable[[dict[str, str]], None],
    *,
    thinking_text: str,
    partial_text: str,
) -> None:
    """Invoke a provider progress callback on a best-effort basis.

    Callback failures are swallowed so a broken GUI transport cannot abort
    an in-flight provider stream.

    Args:
        progress_callback: The callable to invoke with progress data.
        thinking_text: Accumulated thinking-channel text so far.
        partial_text: Accumulated answer-channel text so far.
    """
    try:
        progress_callback(
            {
                "status": "thinking",
                "thinking_text": thinking_text,
                "partial_text": partial_text,
            }
        )
    except Exception:
        pass


def emit_progress_if_needed(
    progress_callback: Callable[[dict[str, str]], None],
    current_thinking: str,
    current_answer: str,
    last_emit_at: float,
    last_sent_thinking: str,
    last_sent_answer: str,
) -> tuple[float, str, str]:
    """Send a progress callback if enough content has changed.

    Applies rate-limiting so the callback fires at most every
    ``PROGRESS_EMIT_MIN_INTERVAL_SECONDS`` unless at least
    ``PROGRESS_EMIT_MIN_NEW_CHARS`` characters were added to either channel.

    Args:
        progress_callback: The callable to invoke with progress data.
        current_thinking: Accumulated thinking text so far.
        current_answer: Accumulated answer text so far.
        last_emit_at: Monotonic timestamp of the last emission.
        last_sent_thinking: Thinking text sent in the last emission.
        last_sent_answer: Answer text sent in the last emission.

    Returns:
        Updated ``(last_emit_at, last_sent_thinking, last_sent_answer)``.
    """
    if not current_thinking and not current_answer:
        return last_emit_at, last_sent_thinking, last_sent_answer

    changed = (
        current_thinking != last_sent_thinking
        or current_answer != last_sent_answer
    )
    if not changed:
        return last_emit_at, last_sent_thinking, last_sent_answer

    now = time.monotonic()
    if now - last_emit_at < PROGRESS_EMIT_MIN_INTERVAL_SECONDS and (
        len(current_thinking) - len(last_sent_thinking) < PROGRESS_EMIT_MIN_NEW_CHARS
        and len(current_answer) - len(last_sent_answer) < PROGRESS_EMIT_MIN_NEW_CHARS
    ):
        return last_emit_at, last_sent_thinking, last_sent_answer

    emit_progress(
        progress_callback=progress_callback,
        thinking_text=current_thinking,
        partial_text=current_answer,
    )
    return now, current_thinking, current_answer


def stream_progress_chunks(
    chunks: Iterable[Any],
    progress_callback: Callable[[dict[str, str]], None],
    thinking_parts: list[str],
    answer_parts: list[str],
) -> Iterator[Any]:
    """Yield stream chunks while accumulating channels and emitting progress.

    Each chunk's reasoning and answer text is appended to the caller-owned
    accumulator lists, throttled progress updates are sent through
    ``emit_progress_if_needed``, and the unmodified chunk is yielded so the
    shared retry plumbing can keep tracking partial output. When the stream
    is exhausted, one final progress update is emitted if any text arrived.

    Args:
        chunks: Iterable of string-compatible stream chunks with separated
            answer/reasoning channels.
        progress_callback: The callable to invoke with progress data.
        thinking_parts: Caller-owned list collecting thinking-channel text.
        answer_parts: Caller-owned list collecting answer-channel text.

    Yields:
        The unmodified input chunks.
    """
    last_emit_at = 0.0
    last_sent_thinking = ""
    last_sent_answer = ""

    for chunk in chunks:
        thinking_delta = stream_chunk_reasoning_text(chunk)
        answer_delta = stream_chunk_answer_text(chunk)
        if thinking_delta:
            thinking_parts.append(thinking_delta)
        if answer_delta:
            answer_parts.append(answer_delta)

        current_thinking = "".join(thinking_parts).strip()
        current_answer = _clean_streamed_answer_text(
            answer_text="".join(answer_parts),
            thinking_text=current_thinking,
        )
        last_emit_at, last_sent_thinking, last_sent_answer = emit_progress_if_needed(
            progress_callback=progress_callback,
            current_thinking=current_thinking,
            current_answer=current_answer,
            last_emit_at=last_emit_at,
            last_sent_thinking=last_sent_thinking,
            last_sent_answer=last_sent_answer,
        )
        yield chunk

    final_thinking = "".join(thinking_parts).strip()
    final_answer = _clean_streamed_answer_text(
        answer_text="".join(answer_parts),
        thinking_text=final_thinking,
    )
    if final_thinking or final_answer:
        emit_progress(
            progress_callback=progress_callback,
            thinking_text=final_thinking,
            partial_text=final_answer,
        )


def finalize_progress_stream_response(
    thinking_parts: list[str],
    answer_parts: list[str],
    *,
    empty_response_message: str,
) -> str:
    """Assemble and validate the final streamed answer text.

    Reasoning-channel text never becomes answer output: when only thinking
    text was streamed, the response is treated as empty.

    Args:
        thinking_parts: Collected thinking-channel text fragments.
        answer_parts: Collected answer-channel text fragments.
        empty_response_message: Provider-specific user-facing error message
            raised when no answer text was produced.

    Returns:
        The cleaned final answer text.

    Raises:
        AIProviderError: If the answer channel is empty after cleaning.
    """
    final_thinking = "".join(thinking_parts).strip()
    final_answer = _clean_streamed_answer_text(
        answer_text="".join(answer_parts),
        thinking_text=final_thinking,
    )
    if final_answer:
        return final_answer
    raise AIProviderError(empty_response_message)
