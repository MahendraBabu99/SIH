"""Tests for chat streaming channel separation."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from unittest.mock import patch

from app.ai_providers import StreamedResponseChunk
import app.routes.tasks as tasks
import app.routes.tasks_chat as tasks_chat


class _AuditRecorder:
    """Collect audit log calls made during a chat run."""

    def __init__(self) -> None:
        """Initialize the in-memory audit call list."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def log(self, action: str, details: dict[str, Any]) -> None:
        """Record an audit action and details.

        Args:
            action: Audit action name.
            details: Audit details payload.
        """
        self.calls.append((action, details))


class _ReasoningChatProvider:
    """Provider stub that streams reasoning separately from answer text."""

    def analyze_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> Iterator[StreamedResponseChunk]:
        """Yield a reasoning chunk followed by an answer chunk.

        Args:
            system_prompt: System prompt supplied by chat task.
            user_prompt: User prompt supplied by chat task.
            max_tokens: Maximum answer token budget.

        Yields:
            Separated stream chunks.
        """
        del system_prompt, user_prompt, max_tokens
        yield StreamedResponseChunk(reasoning_text="hidden reasoning")
        yield StreamedResponseChunk(answer_text="Visible answer.")


def test_chat_stream_emits_reasoning_without_persisting_it() -> None:
    """Chat SSE exposes GUI reasoning while history and audit stay answer-only."""
    with TemporaryDirectory(prefix="aift-chat-reasoning-") as temp_dir:
        case_dir = Path(temp_dir)
        parsed_dir = case_dir / "parsed"
        parsed_dir.mkdir()
        audit = _AuditRecorder()
        case = {
            "case_dir": str(case_dir),
            "audit": audit,
            "image_metadata": {},
            "image_states": {},
        }
        events: list[dict[str, Any]] = []

        with (
            patch.object(tasks_chat, "get_case", return_value=case),
            patch.object(tasks_chat, "get_cancel_event", return_value=None),
            patch.object(tasks_chat, "set_progress_status", lambda *_args, **_kwargs: None),
            patch.object(tasks_chat, "emit_progress", lambda _collection, _case_id, event: events.append(event)),
            patch.object(tasks_chat, "create_provider", return_value=_ReasoningChatProvider()),
            patch.object(
                tasks,
                "load_case_analysis_results",
                return_value={
                    "images": {
                        "img1": {
                            "label": "Image 1",
                            "summary": "result",
                            "per_artifact": [],
                        },
                    },
                    "cross_image_summary": None,
                },
            ),
            patch.object(tasks, "resolve_case_investigation_context", return_value={}),
            patch.object(tasks, "resolve_case_parsed_dir", return_value=parsed_dir),
            patch.object(tasks_chat, "collect_case_image_csv_paths", return_value=[]),
        ):
            tasks_chat.run_chat(
                case_id="case-reasoning",
                message="What happened?",
                config_snapshot={"analysis": {"ai_max_tokens": 1000}},
            )

        assert {"type": "reasoning", "content": "hidden reasoning"} in events
        assert {"type": "token", "content": "Visible answer."} in events
        assert any(event.get("type") == "done" for event in events)

        history_lines = (case_dir / "chat_history.jsonl").read_text(encoding="utf-8").splitlines()
        history = [json.loads(line) for line in history_lines]
        assert history[-1]["role"] == "assistant"
        assert history[-1]["content"] == "Visible answer."
        assert "hidden reasoning" not in json.dumps(history)
        assert "hidden reasoning" not in json.dumps(audit.calls)
