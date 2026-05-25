"""Cross-provider contract tests for AIFT AI provider behavior."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.ai_providers import AIProviderError, ClaudeProvider, KimiProvider, LocalProvider, OpenAIProvider


def _make_openai_response(text: str) -> SimpleNamespace:
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _make_anthropic_response(text: str) -> SimpleNamespace:
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


@contextmanager
def _provider_context(provider_name: str, mock_client: MagicMock):
    if provider_name == "claude":
        with patch("anthropic.Anthropic", return_value=mock_client):
            yield ClaudeProvider(api_key="sk-test", attach_csv_as_file=False)
        return

    with patch("openai.OpenAI", return_value=mock_client):
        if provider_name == "openai":
            yield OpenAIProvider(api_key="sk-test", model="gpt-4o", attach_csv_as_file=False)
        elif provider_name == "kimi":
            yield KimiProvider(api_key="sk-test", attach_csv_as_file=False)
        elif provider_name == "local":
            yield LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
                attach_csv_as_file=False,
            )
        else:
            raise AssertionError(f"unknown provider {provider_name}")


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_non_streaming_empty_response_contract(provider_name: str) -> None:
    mock_client = MagicMock()
    if provider_name == "claude":
        mock_client.messages.create.return_value = SimpleNamespace(content=[])
    else:
        mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[])

    with _provider_context(provider_name, mock_client) as provider:
        with pytest.raises(AIProviderError, match="empty"):
            provider.analyze("system", "user")


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_streaming_empty_response_contract(provider_name: str) -> None:
    mock_client = MagicMock()
    if provider_name == "claude":
        mock_client.messages.create.return_value = [SimpleNamespace(type="message_stop")]
    else:
        mock_client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace())])
        ]

    with _provider_context(provider_name, mock_client) as provider:
        with pytest.raises(AIProviderError, match="empty"):
            list(provider.analyze_stream("system", "user"))


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_attachment_disabled_still_inlines_contract(provider_name: str) -> None:
    mock_client = MagicMock()
    if provider_name == "claude":
        mock_client.messages.create.return_value = _make_anthropic_response("result")
    else:
        mock_client.chat.completions.create.return_value = _make_openai_response("result")

    with TemporaryDirectory(prefix="aift-provider-contract-") as temp_dir:
        csv_path = Path(temp_dir) / "evidence.csv"
        csv_path.write_text("ts,name\n2026-01-15,EntryA\n", encoding="utf-8")

        with _provider_context(provider_name, mock_client) as provider:
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[{"path": str(csv_path), "name": "evidence.csv", "mime_type": "text/csv"}],
            )

    assert result == "result"
    if provider_name == "claude":
        prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    else:
        prompt = mock_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "File attachments were unavailable" in prompt
    assert "--- BEGIN ATTACHMENT: evidence.csv ---" in prompt
    assert "ts,name" in prompt
