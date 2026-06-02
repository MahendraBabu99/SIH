"""Cross-provider contract tests for AIFT AI provider behavior."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.ai_providers import (
    AIProviderError,
    ClaudeProvider,
    KimiProvider,
    LocalProvider,
    OpenAIProvider,
    stream_chunk_answer_text,
    stream_chunk_reasoning_text,
)
from app.ai_providers.base import _RATE_LIMIT_STATE


_PROVIDER_DISPLAY_NAMES = {
    "claude": "Claude",
    "openai": "OpenAI",
    "kimi": "Kimi",
    "local": "Local/OpenAI-compatible",
}
_OPENAI_COMPATIBLE_PROVIDERS = ["openai", "kimi", "local"]
_REASONING_STREAM_PROVIDERS = ["claude", "openai", "kimi", "local"]


def _make_openai_response(text: str) -> SimpleNamespace:
    """Build a minimal OpenAI-compatible response with answer content.

    Args:
        text: Answer-channel content for the response.

    Returns:
        A minimal response object shaped like an OpenAI Chat Completion.
    """
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _make_anthropic_response(text: str) -> SimpleNamespace:
    """Build a minimal Anthropic response with text content.

    Args:
        text: Text content for the response.

    Returns:
        A minimal response object shaped like an Anthropic Message.
    """
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


def _make_openai_reasoning_response(reasoning_text: str) -> SimpleNamespace:
    """Build an OpenAI-compatible response that contains only reasoning text.

    Args:
        reasoning_text: Hidden reasoning-channel text for the response.

    Returns:
        A minimal response object with no answer-channel content.
    """
    message = SimpleNamespace(content="", reasoning_content=reasoning_text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _make_stream_chunk(
    provider_name: str,
    *,
    content: str | None = None,
    reasoning: str | None = None,
    refusal: str | None = None,
) -> SimpleNamespace:
    """Build a provider-shaped stream chunk for contract tests.

    Args:
        provider_name: Provider key used by the parametrized test.
        content: Optional answer-channel stream text.
        reasoning: Optional reasoning-channel stream text.
        refusal: Optional refusal-channel stream text.

    Returns:
        A minimal provider stream chunk.
    """
    if provider_name == "claude":
        if reasoning is not None:
            return SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking=reasoning),
            )
        return SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=content or ""),
        )

    delta_values: dict[str, str] = {}
    if content is not None:
        delta_values["content"] = content
    if reasoning is not None:
        delta_values["reasoning_content"] = reasoning
    if refusal is not None:
        delta_values["refusal"] = refusal
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**delta_values))])


def _raise_after_chunks(chunks: list[SimpleNamespace], error: Exception) -> Iterator[SimpleNamespace]:
    """Yield stream chunks and then raise an error.

    Args:
        chunks: Stream chunks to yield before failing.
        error: Exception to raise after yielding ``chunks``.

    Yields:
        The supplied stream chunks.

    Raises:
        Exception: Always raises ``error`` after the chunks are yielded.
    """
    yield from chunks
    raise error


@contextmanager
def _provider_context(
    provider_name: str,
    mock_client: MagicMock,
    *,
    attach_csv_as_file: bool = False,
) -> Iterator[ClaudeProvider | OpenAIProvider | KimiProvider | LocalProvider]:
    """Create a provider with its SDK client patched to a mock.

    Args:
        provider_name: Provider key used by the parametrized test.
        mock_client: Mock SDK client returned by the provider constructor.
        attach_csv_as_file: Whether the provider should attempt file mode.

    Yields:
        The configured provider instance.
    """
    if provider_name == "claude":
        with patch("anthropic.Anthropic", return_value=mock_client):
            yield ClaudeProvider(api_key="sk-test", attach_csv_as_file=attach_csv_as_file)
        return

    with patch("openai.OpenAI", return_value=mock_client):
        if provider_name == "openai":
            yield OpenAIProvider(
                api_key="sk-test",
                model="gpt-4o",
                attach_csv_as_file=attach_csv_as_file,
            )
        elif provider_name == "kimi":
            yield KimiProvider(api_key="sk-test", attach_csv_as_file=attach_csv_as_file)
        elif provider_name == "local":
            yield LocalProvider(
                base_url="http://localhost:11434/v1",
                model="test",
                attach_csv_as_file=attach_csv_as_file,
            )
        else:
            raise AssertionError(f"unknown provider {provider_name}")


@contextmanager
def _rate_limit_context(provider_name: str, error_type: type[Exception]) -> Iterator[MagicMock]:
    """Patch provider rate-limit classes and sleep for retry tests.

    Args:
        provider_name: Provider key used by the parametrized test.
        error_type: Exception class to treat as the provider rate-limit error.

    Yields:
        The patched ``time.sleep`` mock.
    """
    _RATE_LIMIT_STATE.pop(_PROVIDER_DISPLAY_NAMES[provider_name], None)
    sleep_patch = patch("app.ai_providers.base.time.sleep")
    if provider_name == "claude":
        with patch("anthropic.RateLimitError", error_type), sleep_patch as mock_sleep:
            yield mock_sleep
        return

    with patch("openai.RateLimitError", error_type), sleep_patch as mock_sleep:
        yield mock_sleep


@pytest.fixture(autouse=True)
def _clear_provider_rate_limit_state() -> Iterator[None]:
    """Clear provider rate-limit state before and after each contract test.

    Yields:
        ``None`` while the test executes.
    """
    for provider_display_name in _PROVIDER_DISPLAY_NAMES.values():
        _RATE_LIMIT_STATE.pop(provider_display_name, None)
    yield
    for provider_display_name in _PROVIDER_DISPLAY_NAMES.values():
        _RATE_LIMIT_STATE.pop(provider_display_name, None)


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_streaming_answer_content_is_delivered_contract(provider_name: str) -> None:
    """Providers stream answer-channel text chunks in order."""
    mock_client = MagicMock()
    stream_chunks = [
        _make_stream_chunk(provider_name, content="Chunk A "),
        _make_stream_chunk(provider_name, content="Chunk B"),
    ]
    if provider_name == "claude":
        mock_client.messages.create.return_value = stream_chunks
    else:
        mock_client.chat.completions.create.return_value = stream_chunks

    with _provider_context(provider_name, mock_client) as provider:
        chunks = list(provider.analyze_stream("system", "user"))

    assert chunks == ["Chunk A ", "Chunk B"]


@pytest.mark.parametrize("provider_name", _REASONING_STREAM_PROVIDERS)
def test_streaming_reasoning_deltas_are_not_delivered_contract(provider_name: str) -> None:
    """Provider streams keep reasoning separate from answers."""
    mock_client = MagicMock()
    stream_chunks = [
        _make_stream_chunk(provider_name, reasoning="hidden reasoning "),
        _make_stream_chunk(provider_name, content="Visible answer."),
        _make_stream_chunk(provider_name, reasoning="more hidden reasoning"),
    ]
    if provider_name == "claude":
        mock_client.messages.create.return_value = stream_chunks
    else:
        mock_client.chat.completions.create.return_value = stream_chunks

    with _provider_context(provider_name, mock_client) as provider:
        chunks = list(provider.analyze_stream("system", "user"))

    assert "".join(stream_chunk_answer_text(chunk) for chunk in chunks) == "Visible answer."
    assert "".join(stream_chunk_reasoning_text(chunk) for chunk in chunks) == (
        "hidden reasoning more hidden reasoning"
    )
    assert "hidden reasoning" not in "".join(str(chunk) for chunk in chunks)


@pytest.mark.parametrize("provider_name", _REASONING_STREAM_PROVIDERS)
def test_streaming_reasoning_only_output_is_empty_contract(provider_name: str) -> None:
    """Reasoning-only streams expose GUI reasoning but fail final answer output."""
    mock_client = MagicMock()
    stream_chunks = [_make_stream_chunk(provider_name, reasoning="hidden reasoning only")]
    if provider_name == "claude":
        mock_client.messages.create.return_value = stream_chunks
    else:
        mock_client.chat.completions.create.return_value = stream_chunks

    with _provider_context(provider_name, mock_client) as provider:
        stream = provider.analyze_stream("system", "user")
        first_chunk = next(stream)
        assert stream_chunk_answer_text(first_chunk) == ""
        assert stream_chunk_reasoning_text(first_chunk) == "hidden reasoning only"
        assert str(first_chunk) == ""
        with pytest.raises(AIProviderError, match="empty"):
            next(stream)


@pytest.mark.parametrize("provider_name", _OPENAI_COMPATIBLE_PROVIDERS)
def test_non_streaming_reasoning_only_output_is_empty_contract(provider_name: str) -> None:
    """Reasoning-only non-streaming responses fail like reasoning-only streams."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_reasoning_response(
        "hidden reasoning only"
    )

    with _provider_context(provider_name, mock_client) as provider:
        with pytest.raises(AIProviderError, match="empty"):
            provider.analyze("system", "user")


def test_local_non_streaming_leading_reasoning_markup_only_is_empty_contract() -> None:
    """Local reasoning markup in content is not answer-channel text."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_response(
        "<think>hidden reasoning only</think>"
    )

    with _provider_context("local", mock_client) as provider:
        with pytest.raises(AIProviderError, match="empty"):
            provider.analyze("system", "user")


def test_local_non_streaming_unterminated_leading_reasoning_is_empty_contract() -> None:
    """Truncated local reasoning markup in content is not answer-channel text."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_response(
        "<thinking>hidden reasoning before truncation\nPotential answer"
    )

    with _provider_context("local", mock_client) as provider:
        with pytest.raises(AIProviderError, match="empty") as exc_info:
            provider.analyze("system", "user")

    assert "hidden reasoning before truncation" not in str(exc_info.value)


@pytest.mark.parametrize("provider_name", _OPENAI_COMPATIBLE_PROVIDERS)
def test_streaming_refusals_raise_provider_error_contract(provider_name: str) -> None:
    """OpenAI-compatible stream refusals surface as provider errors."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = [
        _make_stream_chunk(provider_name, refusal="No analysis available."),
    ]

    with _provider_context(provider_name, mock_client) as provider:
        with pytest.raises(AIProviderError, match="refused"):
            list(provider.analyze_stream("system", "user"))


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_streaming_retries_pre_output_rate_limits_contract(provider_name: str) -> None:
    """Providers retry a stream rate limit before any answer text is yielded."""
    class _FakeRateLimitError(Exception):
        """Fake provider rate-limit error for pre-output retry tests."""

    mock_client = MagicMock()
    recovered_stream = [_make_stream_chunk(provider_name, content="Recovered")]
    if provider_name == "claude":
        mock_client.messages.create.side_effect = [
            _FakeRateLimitError("rate limited before stream"),
            recovered_stream,
        ]
    else:
        mock_client.chat.completions.create.side_effect = [
            _FakeRateLimitError("rate limited before stream"),
            recovered_stream,
        ]

    with _rate_limit_context(provider_name, _FakeRateLimitError) as mock_sleep:
        with _provider_context(provider_name, mock_client) as provider:
            chunks = list(provider.analyze_stream("system", "user"))

    assert chunks == ["Recovered"]
    assert mock_sleep.called


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_streaming_does_not_retry_mid_output_rate_limits_contract(provider_name: str) -> None:
    """Providers stop instead of replaying a stream after partial output."""
    class _FakeRateLimitError(Exception):
        """Fake provider rate-limit error for mid-output retry tests."""

    mock_client = MagicMock()
    rate_limit_error = _FakeRateLimitError("rate limited after first chunk")
    failing_stream = _raise_after_chunks(
        [_make_stream_chunk(provider_name, content="First chunk")],
        rate_limit_error,
    )
    if provider_name == "claude":
        mock_client.messages.create.return_value = failing_stream
    else:
        mock_client.chat.completions.create.return_value = failing_stream

    with _rate_limit_context(provider_name, _FakeRateLimitError) as mock_sleep:
        with _provider_context(provider_name, mock_client) as provider:
            stream = provider.analyze_stream("system", "user")
            assert next(stream) == "First chunk"
            with pytest.raises(AIProviderError, match="partial output"):
                next(stream)

    if provider_name == "claude":
        assert mock_client.messages.create.call_count == 1
    else:
        assert mock_client.chat.completions.create.call_count == 1
    assert not mock_sleep.called


@pytest.mark.parametrize("provider_name", _REASONING_STREAM_PROVIDERS)
def test_streaming_does_not_retry_after_reasoning_rate_limits_contract(provider_name: str) -> None:
    """Visible GUI reasoning also counts as partial stream output."""
    class _FakeRateLimitError(Exception):
        """Fake provider rate-limit error after reasoning output."""

    mock_client = MagicMock()
    rate_limit_error = _FakeRateLimitError("rate limited after reasoning")
    failing_stream = _raise_after_chunks(
        [_make_stream_chunk(provider_name, reasoning="reasoning before failure")],
        rate_limit_error,
    )
    if provider_name == "claude":
        mock_client.messages.create.return_value = failing_stream
    else:
        mock_client.chat.completions.create.return_value = failing_stream

    with _rate_limit_context(provider_name, _FakeRateLimitError) as mock_sleep:
        with _provider_context(provider_name, mock_client) as provider:
            stream = provider.analyze_stream("system", "user")
            first_chunk = next(stream)
            assert stream_chunk_answer_text(first_chunk) == ""
            assert stream_chunk_reasoning_text(first_chunk) == "reasoning before failure"
            with pytest.raises(AIProviderError, match="partial output"):
                next(stream)

    if provider_name == "claude":
        assert mock_client.messages.create.call_count == 1
    else:
        assert mock_client.chat.completions.create.call_count == 1
    assert not mock_sleep.called


def test_local_streaming_leading_reasoning_markup_is_separated_contract() -> None:
    """Local streamed think blocks are GUI reasoning, not answer text."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = [
        _make_stream_chunk("local", content="<think>hidden "),
        _make_stream_chunk("local", content="reasoning</think>\nVisible "),
        _make_stream_chunk("local", content="answer."),
    ]

    with _provider_context("local", mock_client) as provider:
        chunks = list(provider.analyze_stream("system", "user"))

    assert "".join(stream_chunk_answer_text(chunk) for chunk in chunks) == "Visible answer."
    assert "".join(stream_chunk_reasoning_text(chunk) for chunk in chunks) == "hidden reasoning"
    assert "hidden reasoning" not in "".join(str(chunk) for chunk in chunks)


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
@pytest.mark.parametrize("attach_csv_as_file", [False, True])
def test_requested_attachments_must_be_readable_contract(
    provider_name: str,
    attach_csv_as_file: bool,
) -> None:
    """Providers fail when a requested attachment path is not readable."""
    mock_client = MagicMock()
    with TemporaryDirectory(prefix="aift-provider-contract-") as temp_dir:
        missing_path = Path(temp_dir) / "missing-evidence.csv"

        with _provider_context(
            provider_name,
            mock_client,
            attach_csv_as_file=attach_csv_as_file,
        ) as provider:
            with pytest.raises(AIProviderError, match="not readable"):
                provider.analyze_with_attachments(
                    "system",
                    "user",
                    attachments=[
                        {
                            "path": str(missing_path),
                            "name": "missing-evidence.csv",
                            "mime_type": "text/csv",
                        }
                    ],
                )

    if provider_name == "claude":
        mock_client.messages.create.assert_not_called()
    else:
        mock_client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("provider_name", _OPENAI_COMPATIBLE_PROVIDERS)
def test_uploaded_files_are_cleaned_up_when_file_mode_falls_back_contract(
    provider_name: str,
) -> None:
    """OpenAI-compatible providers delete uploads when file mode falls back."""
    mock_client = MagicMock()
    mock_client.files.create.return_value = SimpleNamespace(id="file-cleanup")
    mock_client.responses.create.side_effect = RuntimeError("unrecognized request url /responses")
    mock_client.chat.completions.create.return_value = _make_openai_response("fallback result")

    with TemporaryDirectory(prefix="aift-provider-contract-") as temp_dir:
        csv_path = Path(temp_dir) / "evidence.csv"
        csv_path.write_text("ts,name\n2026-01-15,EntryA\n", encoding="utf-8")

        with _provider_context(
            provider_name,
            mock_client,
            attach_csv_as_file=True,
        ) as provider:
            result = provider.analyze_with_attachments(
                "system",
                "user",
                attachments=[
                    {
                        "path": str(csv_path),
                        "name": "evidence.csv",
                        "mime_type": "text/csv",
                    }
                ],
            )

    assert result == "fallback result"
    mock_client.files.delete.assert_called_once_with("file-cleanup")


@pytest.mark.parametrize("provider_name", ["claude", "openai", "kimi", "local"])
def test_non_streaming_empty_response_contract(provider_name: str) -> None:
    """Providers raise a provider error for empty non-streaming responses."""
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
    """Providers raise a provider error for empty streaming responses."""
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
    """Providers inline readable attachments when file mode is disabled."""
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
