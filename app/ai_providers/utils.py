"""Text extraction, attachment handling, and response processing utilities.

This module contains all functions for extracting text from AI provider
responses (Anthropic and OpenAI formats), normalizing and inlining file
attachments, stripping reasoning blocks from local model output, and
shared OpenAI-compatible stream-channel and Responses API file-upload logic
used by OpenAI, Kimi, and Local providers.

Attributes:
    _LEADING_REASONING_BLOCK_RE: Regex pattern matching leading ``<think>``,
        ``<thinking>``, ``<reasoning>`` XML blocks or fenced code blocks.
    _OPENAI_REASONING_DELTA_FIELDS: OpenAI-compatible streaming field names
        that contain hidden reasoning or thinking text rather than answer text.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_LEADING_REASONING_BLOCK_RE = re.compile(
    r"^\s*(?:"
    r"(?:<\s*(?:think|thinking|reasoning)\b[^>]*>.*?<\s*/\s*(?:think|thinking|reasoning)\s*>\s*)"
    r"|(?:```(?:think|thinking|reasoning)[^\n]*\n.*?```\s*)"
    r")+",
    flags=re.IGNORECASE | re.DOTALL,
)
_OPENAI_REASONING_DELTA_FIELDS = ("reasoning_content", "reasoning", "thinking")
_STREAM_REASONING_START_RE = re.compile(
    r"^\s*<\s*(?P<tag>think|thinking|reasoning)\b[^>]*>",
    flags=re.IGNORECASE,
)
_STREAM_REASONING_TAG_PREFIXES = (
    "<",
    "<t",
    "<th",
    "<thi",
    "<thin",
    "<think",
    "<thinki",
    "<thinkin",
    "<thinking",
    "<r",
    "<re",
    "<rea",
    "<reas",
    "<reaso",
    "<reason",
    "<reasoni",
    "<reasonin",
    "<reasoning",
)


class StreamedResponseChunk(str):
    """String-compatible provider stream chunk with separated text channels.

    The string value is always the answer-channel text, so CLI/headless
    callers that print, join, serialize, or log chunks continue to see only
    final-answer content. GUI code may explicitly read ``reasoning_text`` to
    display model thinking in a separate collapsible panel.

    Args:
        answer_text: User-visible answer-channel text.
        reasoning_text: Provider reasoning or thinking text that must remain
            separate from normal answer output.

    Attributes:
        reasoning_text: Provider reasoning or thinking text for GUI-only
            display.
    """

    __slots__ = ("reasoning_text",)

    def __new__(
        cls,
        answer_text: Any = "",
        reasoning_text: Any = "",
    ) -> "StreamedResponseChunk":
        """Create a stream chunk whose string value is answer text.

        Args:
            answer_text: User-visible answer-channel text.
            reasoning_text: GUI-only reasoning or thinking text.

        Returns:
            A string-compatible stream chunk.
        """
        instance = str.__new__(cls, str(answer_text or ""))
        instance.reasoning_text = str(reasoning_text or "")
        return instance

    @property
    def answer_text(self) -> str:
        """Return answer-channel text safe for normal outputs."""
        return str(self)

    def has_answer(self) -> bool:
        """Return whether this chunk carries answer-channel text."""
        return bool(self.answer_text)

    def has_reasoning(self) -> bool:
        """Return whether this chunk carries reasoning-channel text."""
        return bool(self.reasoning_text)


class _LeadingReasoningStreamSplitter:
    """Split leading local-model reasoning markup out of content chunks.

    Some OpenAI-compatible local models stream chain-of-thought in the
    answer ``content`` field as a leading ``<think>``, ``<thinking>``, or
    ``<reasoning>`` block. This stateful splitter moves those leading
    blocks into the reasoning channel while letting later answer text
    pass through unchanged.
    """

    def __init__(self) -> None:
        """Initialize the parser state for a new model stream."""
        self._buffer = ""
        self._state = "undecided"
        self._tag = ""

    def split(self, text: Any) -> StreamedResponseChunk:
        """Split one streamed content fragment into answer/reasoning text.

        Args:
            text: A content-channel fragment from a local model stream.

        Returns:
            A stream chunk with leading reasoning markup separated from
            answer text.
        """
        fragment = str(text or "")
        if not fragment:
            return StreamedResponseChunk()

        self._buffer += fragment
        answer_parts: list[str] = []
        reasoning_parts: list[str] = []

        while self._buffer:
            if self._state == "answer":
                answer_parts.append(self._buffer)
                self._buffer = ""
                break

            if self._state == "undecided":
                start_match = _STREAM_REASONING_START_RE.match(self._buffer)
                if start_match:
                    self._tag = start_match.group("tag").lower()
                    self._buffer = self._buffer[start_match.end():]
                    self._state = "reasoning"
                    continue

                stripped = self._buffer.lstrip()
                if _is_possible_stream_reasoning_start(stripped):
                    break

                self._state = "answer"
                continue

            if self._state == "reasoning":
                close_match = _find_stream_reasoning_close(self._buffer, self._tag)
                if close_match:
                    reasoning_parts.append(self._buffer[: close_match.start()])
                    self._buffer = self._buffer[close_match.end():].lstrip()
                    self._state = "undecided"
                    self._tag = ""
                    continue

                reasoning_text, retained = _split_reasoning_buffer_for_close_prefix(
                    self._buffer,
                    self._tag,
                )
                if reasoning_text:
                    reasoning_parts.append(reasoning_text)
                self._buffer = retained
                break

        return StreamedResponseChunk(
            answer_text="".join(answer_parts),
            reasoning_text="".join(reasoning_parts),
        )

    def flush(self) -> StreamedResponseChunk:
        """Return any buffered text when a stream ends.

        Returns:
            A final separated chunk for residual buffered text.
        """
        if not self._buffer:
            return StreamedResponseChunk()

        if self._state == "reasoning":
            chunk = StreamedResponseChunk(reasoning_text=self._buffer)
        else:
            chunk = StreamedResponseChunk(answer_text=self._buffer)
        self._buffer = ""
        self._state = "answer"
        self._tag = ""
        return chunk


def _is_possible_stream_reasoning_start(text: str) -> bool:
    """Return whether text may be the prefix of a reasoning block opener.

    Args:
        text: Buffered stream text stripped of leading whitespace.

    Returns:
        ``True`` when more streamed text is needed to classify the prefix.
    """
    lowered = text.lower()
    if (
        lowered.startswith(("<think", "<thinking", "<reasoning"))
        and ">" not in lowered
    ):
        return True
    return any(prefix.startswith(lowered) for prefix in _STREAM_REASONING_TAG_PREFIXES)


def _find_stream_reasoning_close(text: str, tag: str) -> re.Match[str] | None:
    """Find the closing tag for a streamed reasoning block.

    Args:
        text: Buffered reasoning text.
        tag: Opening reasoning tag name.

    Returns:
        The closing-tag regex match, or ``None`` if absent.
    """
    return re.search(rf"<\s*/\s*{re.escape(tag)}\s*>", text, flags=re.IGNORECASE)


def _split_reasoning_buffer_for_close_prefix(text: str, tag: str) -> tuple[str, str]:
    """Keep a possible split closing tag buffered for the next stream chunk.

    Args:
        text: Buffered reasoning text without a full closing tag.
        tag: Opening reasoning tag name.

    Returns:
        A ``(reasoning_text, retained_suffix)`` tuple.
    """
    close_tag = f"</{tag}>"
    last_angle = text.rfind("<")
    if last_angle < 0:
        return text, ""

    suffix = text[last_angle:]
    if close_tag.startswith(suffix.lower()):
        return text[:last_angle], suffix
    return text, ""


def stream_chunk_answer_text(chunk: Any) -> str:
    """Return normal answer text from a provider stream chunk.

    Args:
        chunk: A provider stream chunk. Plain strings are treated as
            answer-channel text for backward compatibility.

    Returns:
        Answer-channel text, or an empty string when absent.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, StreamedResponseChunk):
        return chunk.answer_text
    return str(chunk)


def stream_chunk_reasoning_text(chunk: Any) -> str:
    """Return GUI-only reasoning text from a provider stream chunk.

    Args:
        chunk: A provider stream chunk.

    Returns:
        Reasoning-channel text when present, otherwise an empty string.
    """
    if isinstance(chunk, StreamedResponseChunk):
        return chunk.reasoning_text
    return ""


def stream_chunk_has_text(chunk: Any) -> bool:
    """Return whether a stream chunk has answer or reasoning text.

    Args:
        chunk: A provider stream chunk.

    Returns:
        ``True`` when either answer-channel or reasoning-channel text exists.
    """
    return bool(stream_chunk_answer_text(chunk) or stream_chunk_reasoning_text(chunk))


# ---------------------------------------------------------------------------
# Anthropic text extraction
# ---------------------------------------------------------------------------


def _extract_anthropic_text(response: Any) -> str:
    """Extract the concatenated text from an Anthropic Messages API response.

    Iterates over content blocks in the response, collecting text from
    both object-style blocks (with a ``.text`` attribute) and dict-style
    blocks (with a ``"text"`` key).

    Args:
        response: The Anthropic ``Message`` response object.

    Returns:
        The joined text content, stripped of whitespace.
    """
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
            continue

        if isinstance(block, dict):
            block_text = block.get("text")
            if isinstance(block_text, str):
                chunks.append(block_text)

    return "".join(chunks).strip()


def _extract_anthropic_stream_text(event: Any) -> str:
    """Extract text deltas from Anthropic streamed events.

    Handles ``content_block_delta``, ``content_block_start``, and generic
    delta events from the Anthropic streaming API.

    Args:
        event: A single streamed event from the Anthropic Messages API.

    Returns:
        The text delta string, or empty string if no text content.
    """
    return _split_anthropic_stream_event_text(event).answer_text


def _read_stream_field(payload: Any, field_name: str) -> Any:
    """Read a field from an object-style or dict-style stream payload.

    Args:
        payload: Stream event, content block, or delta payload.
        field_name: Field name to read.

    Returns:
        The field value, or ``None`` if absent.
    """
    value = getattr(payload, field_name, None)
    if value is None and isinstance(payload, dict):
        value = payload.get(field_name)
    return value


def _stream_text_field(payload: Any, field_names: tuple[str, ...]) -> str:
    """Extract the first string field from a stream payload.

    Args:
        payload: Stream event, content block, or delta payload.
        field_names: Field names to check in order.

    Returns:
        The first non-empty string value.
    """
    for field_name in field_names:
        value = _read_stream_field(payload, field_name)
        if isinstance(value, str) and value:
            return value
    return ""


def _split_anthropic_stream_event_text(event: Any) -> StreamedResponseChunk:
    """Split an Anthropic stream event into answer and reasoning channels.

    Args:
        event: A single streamed event from the Anthropic Messages API.

    Returns:
        A string-compatible chunk containing answer text or GUI-only
        reasoning text.
    """
    if event is None:
        return StreamedResponseChunk()

    event_type = _read_stream_field(event, "type")

    if event_type == "content_block_delta":
        delta = _read_stream_field(event, "delta")
        delta_type = str(_read_stream_field(delta, "type") or "").lower()
        if delta_type == "thinking_delta":
            return StreamedResponseChunk(
                reasoning_text=_stream_text_field(delta, ("thinking", "text")),
            )
        if delta_type == "text_delta":
            return StreamedResponseChunk(answer_text=_stream_text_field(delta, ("text",)))
        reasoning_text = _stream_text_field(delta, ("thinking",))
        if reasoning_text:
            return StreamedResponseChunk(reasoning_text=reasoning_text)
        return StreamedResponseChunk(answer_text=_stream_text_field(delta, ("text",)))

    if event_type == "content_block_start":
        content_block = _read_stream_field(event, "content_block")
        block_type = str(_read_stream_field(content_block, "type") or "").lower()
        if block_type == "thinking":
            return StreamedResponseChunk(
                reasoning_text=_stream_text_field(content_block, ("thinking", "text")),
            )
        if block_type == "text":
            return StreamedResponseChunk(answer_text=_stream_text_field(content_block, ("text",)))
        reasoning_text = _stream_text_field(content_block, ("thinking",))
        if reasoning_text:
            return StreamedResponseChunk(reasoning_text=reasoning_text)
        return StreamedResponseChunk(answer_text=_stream_text_field(content_block, ("text",)))

    delta = _read_stream_field(event, "delta")
    if delta is not None:
        reasoning_text = _stream_text_field(delta, ("thinking",))
        if reasoning_text:
            return StreamedResponseChunk(reasoning_text=reasoning_text)
        return StreamedResponseChunk(answer_text=_stream_text_field(delta, ("text",)))

    return StreamedResponseChunk()


# ---------------------------------------------------------------------------
# OpenAI text extraction
# ---------------------------------------------------------------------------


def _coerce_openai_text(value: Any) -> str:
    """Normalize OpenAI-compatible response text payloads into plain strings.

    Handles string values, lists of text items (objects or dicts), and
    returns an empty string for unsupported types.

    Args:
        value: A text value from an OpenAI-compatible response.

    Returns:
        The concatenated plain text string.
    """
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            item_text = getattr(item, "text", None)
            if isinstance(item_text, str):
                parts.append(item_text)
                continue
            if isinstance(item, dict):
                dict_text = item.get("text")
                if isinstance(dict_text, str):
                    parts.append(dict_text)
                    continue
                dict_content = item.get("content")
                if isinstance(dict_content, str):
                    parts.append(dict_content)
        return "".join(parts)

    return ""


def _extract_openai_text(response: Any) -> str:
    """Extract the generated text from an OpenAI Chat Completions API response.

    Handles plain string content and structured content arrays. Hidden
    reasoning fields are deliberately ignored so chain-of-thought or
    thinking text is never returned as user-visible provider output. If the
    model refused the request (non-empty ``refusal`` field), raises an
    ``AIProviderError`` instead of returning the refusal as valid output.

    Args:
        response: The OpenAI ``ChatCompletion`` response object.

    Returns:
        The extracted text content, stripped of whitespace.

    Raises:
        AIProviderError: If the model's ``refusal`` field is non-empty,
            indicating it declined to answer the request.
    """
    choices = getattr(response, "choices", None)
    if not choices:
        return ""

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None and isinstance(first_choice, dict):
        message = first_choice.get("message")

    if message is None:
        return ""

    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")

    if isinstance(content, str):
        stripped_content = content.strip()
        if stripped_content:
            return stripped_content

    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            text = getattr(chunk, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue

            if isinstance(chunk, dict):
                chunk_text = chunk.get("text")
                if isinstance(chunk_text, str):
                    parts.append(chunk_text)
                    continue
                chunk_content = chunk.get("content")
                if isinstance(chunk_content, str):
                    parts.append(chunk_content)
        joined = "".join(parts).strip()
        if joined:
            return joined

    # Check for model refusal before falling back to reasoning fields.
    # The ``refusal`` field is set by OpenAI when the model declines a
    # request.  Returning refusal text as valid analysis output would cause
    # it to appear in forensic reports, so raise an error instead.
    refusal_value = getattr(message, "refusal", None)
    if refusal_value is None and isinstance(message, dict):
        refusal_value = message.get("refusal")
    refusal_text = _coerce_openai_text(refusal_value).strip()
    if refusal_text:
        from .base import AIProviderError

        raise AIProviderError(f"AI model refused the request: {refusal_text}")

    return ""


def _extract_openai_delta_text(delta: Any, field_names: tuple[str, ...]) -> str:
    """Extract streaming delta text for one of the requested fields.

    Args:
        delta: The streaming chunk delta object or dict.
        field_names: Tuple of field names to check in priority order.

    Returns:
        The first non-empty text value found, or empty string.
    """
    if delta is None:
        return ""

    for field_name in field_names:
        value = getattr(delta, field_name, None)
        if value is None and isinstance(delta, dict):
            value = delta.get(field_name)
        text = _coerce_openai_text(value)
        if text:
            return text
    return ""


def _extract_openai_delta_refusal_text(delta: Any) -> str:
    """Extract a model-refusal delta from an OpenAI-compatible stream chunk.

    Args:
        delta: The streaming chunk delta object or dict.

    Returns:
        Refusal text stripped of surrounding whitespace, or an empty string.
    """
    if delta is None:
        return ""

    refusal_value = getattr(delta, "refusal", None)
    if refusal_value is None and isinstance(delta, dict):
        refusal_value = delta.get("refusal")
    return _coerce_openai_text(refusal_value).strip()


def _raise_on_openai_delta_refusal(delta: Any) -> None:
    """Raise the shared provider error when a streamed delta refuses.

    Args:
        delta: The streaming chunk delta object or dict.

    Raises:
        AIProviderError: If the delta contains refusal text.
    """
    refusal_text = _extract_openai_delta_refusal_text(delta)
    if not refusal_text:
        return

    from .base import AIProviderError

    raise AIProviderError(f"AI model refused the request: {refusal_text}")


def _extract_openai_stream_chunk_delta(chunk: Any) -> Any | None:
    """Extract the delta object from an OpenAI-compatible stream chunk.

    Args:
        chunk: A streaming chunk object or dict from Chat Completions.

    Returns:
        The first choice's delta payload, or ``None`` if the chunk has no
        usable delta.
    """
    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")
    if not choices:
        return None

    choice = choices[0]
    delta = getattr(choice, "delta", None)
    if delta is None and isinstance(choice, dict):
        delta = choice.get("delta")
    return delta


def _split_openai_stream_delta_text(delta: Any) -> StreamedResponseChunk:
    """Split an OpenAI-compatible stream delta into output channels.

    Args:
        delta: The stream delta object or dict to inspect.

    Returns:
        A string-compatible chunk containing answer text and separate
        reasoning text.

    Raises:
        AIProviderError: If the delta contains a model refusal.
    """
    _raise_on_openai_delta_refusal(delta)
    return StreamedResponseChunk(
        answer_text=_extract_openai_delta_text(delta, ("content",)),
        reasoning_text=_extract_openai_delta_text(delta, _OPENAI_REASONING_DELTA_FIELDS),
    )


def _extract_openai_responses_text(response: Any) -> str:
    """Extract output text from OpenAI Responses API payloads.

    First attempts the ``output_text`` attribute, then falls back to
    iterating over structured output items.

    Args:
        response: The OpenAI Responses API response object or dict.

    Returns:
        The extracted and stripped text content.
    """
    output_text = getattr(response, "output_text", None)
    text = _coerce_openai_text(output_text).strip()
    if text:
        return text

    output_items = getattr(response, "output", None)
    if output_items is None and isinstance(response, dict):
        output_items = response.get("output")
    if not isinstance(output_items, list):
        return ""

    parts: list[str] = []
    for item in output_items:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            block_type = getattr(block, "type", None)
            if block_type is None and isinstance(block, dict):
                block_type = block.get("type")
            if str(block_type) not in {"output_text", "text"}:
                continue

            block_text = getattr(block, "text", None)
            if block_text is None and isinstance(block, dict):
                block_text = block.get("text")
            normalized = _coerce_openai_text(block_text)
            if normalized:
                parts.append(normalized)

    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Reasoning block handling
# ---------------------------------------------------------------------------


def _strip_leading_reasoning_blocks(text: str) -> str:
    """Remove leading model-thinking blocks from OpenAI-compatible output.

    Some local reasoning models emit ``<think>`` or ``<reasoning>`` blocks
    at the start of their output. This strips those blocks.

    Args:
        text: Raw model output that may begin with reasoning blocks.

    Returns:
        The text with leading reasoning blocks removed.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    return _LEADING_REASONING_BLOCK_RE.sub("", value, count=1).strip()


def _clean_streamed_answer_text(answer_text: str, thinking_text: str) -> str:
    """Drop duplicated streamed thinking text from the final answer channel.

    Args:
        answer_text: The accumulated answer-channel text from streaming.
        thinking_text: The accumulated thinking-channel text from streaming.

    Returns:
        The cleaned answer text with duplicated reasoning removed.
    """
    answer = str(answer_text or "").strip()
    if not answer:
        return ""

    thinking = str(thinking_text or "").strip()
    if thinking and len(thinking) >= 24 and answer.startswith(thinking):
        answer = answer[len(thinking) :].lstrip()

    return _strip_leading_reasoning_blocks(answer)


# ---------------------------------------------------------------------------
# Attachment normalization
# ---------------------------------------------------------------------------


def normalize_attachment_input(attachment: Mapping[str, str] | Any) -> dict[str, str] | None:
    """Validate and normalize a single attachment descriptor.

    Args:
        attachment: A raw attachment descriptor with at least a ``"path"`` key.

    Returns:
        A normalized dict with ``"path"``, ``"name"``, ``"mime_type"`` keys,
        or ``None`` if invalid.
    """
    if not isinstance(attachment, Mapping):
        return None

    path_value = str(attachment.get("path", "")).strip()
    if not path_value:
        return None

    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None

    filename = str(attachment.get("name", "")).strip() or path.name
    mime_type = str(attachment.get("mime_type", "")).strip() or "text/csv"
    return {
        "path": str(path),
        "name": filename,
        "mime_type": mime_type,
    }


def normalize_attachment_inputs(
    attachments: list[Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    """Validate and normalize a list of attachment descriptors.

    Args:
        attachments: Optional list of raw attachment descriptors.

    Returns:
        A list of validated attachment dicts. May be empty.
    """
    normalized: list[dict[str, str]] = []
    for attachment in attachments or []:
        candidate = normalize_attachment_input(attachment)
        if candidate is not None:
            normalized.append(candidate)
    return normalized


def normalize_requested_attachment_inputs(
    attachments: list[Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    """Validate and normalize requested attachments without dropping failures.

    Unlike ``normalize_attachment_inputs``, this helper treats every
    caller-supplied descriptor as required evidence. Missing, malformed, or
    non-file paths raise a provider error so analysis cannot proceed after
    requested evidence disappeared.

    Args:
        attachments: Optional list of raw attachment descriptors.

    Returns:
        A list of normalized attachment dicts. May be empty when no
        attachments were requested.

    Raises:
        AIProviderError: If any requested attachment is malformed or not a
            readable file path.
    """
    from .base import AIProviderError

    normalized: list[dict[str, str]] = []
    for index, attachment in enumerate(attachments or [], start=1):
        if not isinstance(attachment, Mapping):
            raise AIProviderError(
                f"Requested attachment #{index} is invalid; expected a mapping with a file path."
            )

        path_value = str(attachment.get("path", "")).strip()
        attachment_name = str(attachment.get("name", "")).strip()
        display_name = attachment_name or path_value or f"attachment #{index}"
        if not path_value:
            raise AIProviderError(
                f"Requested attachment '{display_name}' is not readable: no file path was provided."
            )

        attachment_path = Path(path_value)
        if not attachment_path.exists():
            raise AIProviderError(
                f"Requested attachment '{display_name}' is not readable: file does not exist at {attachment_path}."
            )
        if not attachment_path.is_file():
            raise AIProviderError(
                f"Requested attachment '{display_name}' is not readable: path is not a file at {attachment_path}."
            )

        normalized.append(
            {
                "path": str(attachment_path),
                "name": attachment_name or attachment_path.name,
                "mime_type": str(attachment.get("mime_type", "")).strip() or "text/csv",
            }
        )
    return normalized


def _prepare_openai_attachment_upload(attachment: Mapping[str, str]) -> tuple[str, str, bool]:
    """Normalize OpenAI attachment upload metadata.

    Some OpenAI Responses API models reject ``.csv`` file extensions.
    This converts CSV metadata to TXT format while keeping contents unchanged.

    Args:
        attachment: A normalized attachment descriptor.

    Returns:
        A 3-tuple of ``(upload_name, upload_mime_type, was_converted)``.
    """
    attachment_path = Path(str(attachment.get("path", "")))
    original_name = str(attachment.get("name", "")).strip() or attachment_path.name or "attachment"
    original_mime_type = str(attachment.get("mime_type", "")).strip() or "text/plain"

    lowered_name = original_name.lower()
    lowered_path_suffix = attachment_path.suffix.lower()
    lowered_mime_type = original_mime_type.lower()
    is_csv_attachment = (
        lowered_name.endswith(".csv")
        or lowered_path_suffix == ".csv"
        or lowered_mime_type in {"text/csv", "application/csv"}
    )
    if not is_csv_attachment:
        return original_name, original_mime_type, False

    stem = Path(original_name).stem or Path(attachment_path.name).stem or "attachment"
    return f"{stem}.txt", "text/plain", True


def _normalize_inline_attachment_text(text: str) -> str:
    """Normalize text for duplicate inline-attachment detection.

    Args:
        text: Prompt or attachment text to normalize.

    Returns:
        The text with BOM and newline differences removed for comparison.
    """
    return str(text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()


def _prompt_already_contains_attachment_text(user_prompt: str, attachment_text: str) -> bool:
    """Return whether the full attachment text is already present in a prompt.

    Args:
        user_prompt: Prompt text that may already contain CSV evidence.
        attachment_text: Attachment file contents.

    Returns:
        ``True`` when the normalized attachment body is already inline.
    """
    normalized_attachment = _normalize_inline_attachment_text(attachment_text)
    if not normalized_attachment:
        return False
    normalized_prompt = _normalize_inline_attachment_text(user_prompt)
    return normalized_attachment in normalized_prompt


def _inline_attachment_data_into_prompt(
    user_prompt: str,
    attachments: list[Mapping[str, str]] | None,
) -> tuple[str, bool]:
    """Append attachment file contents to the user prompt for text-only fallback.

    All attachment data is inlined without truncation -- in DFIR, every row
    matters. When the resulting prompt is too large, the caller uses chunked
    analysis to split it.

    Args:
        user_prompt: The original user prompt text.
        attachments: Optional list of attachment descriptors.

    Returns:
        A 2-tuple of ``(modified_prompt, was_inlined)``.

    Raises:
        AIProviderError: If any requested attachment is missing or cannot be
            read for inlining.
    """
    from .base import AIProviderError

    normalized_attachments = normalize_requested_attachment_inputs(attachments)
    if not normalized_attachments:
        return user_prompt, False

    inline_sections: list[str] = []
    for attachment in normalized_attachments:
        attachment_path = Path(attachment["path"])
        attachment_name = str(attachment.get("name", "")).strip() or attachment_path.name
        try:
            attachment_text = attachment_path.read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
        except OSError as error:
            raise AIProviderError(
                f"Could not read requested attachment '{attachment_name}' at {attachment_path}: {error}"
            ) from error
        if not attachment_text.strip():
            continue
        if _prompt_already_contains_attachment_text(user_prompt, attachment_text):
            logger.info(
                "Skipping inline fallback for attachment '%s' because the prompt already contains its contents.",
                attachment_name,
            )
            continue

        inline_sections.append(
            "\n".join(
                [
                    f"--- BEGIN ATTACHMENT: {attachment_name} ---",
                    attachment_text.rstrip(),
                    f"--- END ATTACHMENT: {attachment_name} ---",
                ]
            )
        )

    if not inline_sections:
        return user_prompt, False

    inlined_prompt = "\n\n".join(
        [
            user_prompt.rstrip(),
            "File attachments were unavailable, so the attachment contents are inlined below.",
            "\n\n".join(inline_sections),
        ]
    ).strip()
    return inlined_prompt, True


def upload_and_request_via_responses_api(
    client: Any,
    openai_module: Any,
    model: str,
    normalized_attachments: list[dict[str, str]],
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    provider_name: str,
    upload_purpose: str = "assistants",
    convert_csv_to_txt: bool = False,
) -> str:
    """Upload attachments and make a Responses API request.

    This is the shared implementation for file-attachment mode used by
    OpenAI, Kimi, and Local providers. Uploads each attachment as a file,
    builds a Responses API request with ``input_file`` references, extracts
    the output text, and cleans up uploaded files.

    Args:
        client: The ``openai.OpenAI`` SDK client instance.
        openai_module: The ``openai`` module (for exception types).
        model: The model identifier to use for the Responses API request.
        normalized_attachments: Validated attachment descriptors.
        system_prompt: The system-level instruction text.
        user_prompt: The user-facing prompt text.
        max_tokens: Maximum completion tokens.
        provider_name: Human-readable provider name for error messages.
        upload_purpose: The ``purpose`` parameter for file uploads.
        convert_csv_to_txt: If ``True``, convert CSV file metadata to TXT
            format before uploading (used by OpenAI).

    Returns:
        The generated text from the Responses API.

    Raises:
        AIProviderError: If the response is empty, an attachment cannot be
            read, or file upload fails.
    """
    from .base import AIProviderError, _resolve_completion_token_retry_limit

    uploaded_file_ids: list[str] = []
    try:
        for attachment in normalized_attachments:
            attachment_path = Path(attachment["path"])

            if convert_csv_to_txt:
                upload_name, upload_mime_type, converted = _prepare_openai_attachment_upload(attachment)
                if converted:
                    logger.debug(
                        "Converting %s attachment upload from CSV to TXT: %s -> %s",
                        provider_name,
                        attachment.get("name", attachment_path.name),
                        upload_name,
                    )
            else:
                upload_name = attachment["name"]
                upload_mime_type = attachment["mime_type"]

            try:
                attachment_bytes = attachment_path.read_bytes()
            except OSError as error:
                attachment_name = str(attachment.get("name", "")).strip() or attachment_path.name
                raise AIProviderError(
                    f"{provider_name} could not read requested attachment "
                    f"'{attachment_name}' at {attachment_path}: {error}"
                ) from error

            uploaded = client.files.create(
                file=(upload_name, attachment_bytes, upload_mime_type),
                purpose=upload_purpose,
            )

            file_id = getattr(uploaded, "id", None)
            if file_id is None and isinstance(uploaded, dict):
                file_id = uploaded.get("id")
            if not isinstance(file_id, str) or not file_id.strip():
                raise AIProviderError(f"{provider_name} file upload returned no file id.")
            uploaded_file_ids.append(file_id)

        user_content: list[dict[str, str]] = [{"type": "input_text", "text": user_prompt}]
        for file_id in uploaded_file_ids:
            user_content.append({"type": "input_file", "file_id": file_id})

        response_request: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": user_content},
            ],
            "max_output_tokens": max_tokens,
        }

        try:
            response = client.responses.create(**response_request)
        except openai_module.BadRequestError as error:
            retry_token_count = _resolve_completion_token_retry_limit(
                error=error,
                requested_tokens=max_tokens,
            )
            if retry_token_count is None:
                raise
            logger.warning(
                "%s rejected max_output_tokens=%d; retrying with max_output_tokens=%d.",
                provider_name,
                max_tokens,
                retry_token_count,
            )
            response_request["max_output_tokens"] = retry_token_count
            response = client.responses.create(**response_request)

        text = _extract_openai_responses_text(response)
        if not text:
            raise AIProviderError(
                f"{provider_name} returned an empty response for file-attachment mode."
            )
        return text
    finally:
        for uploaded_file_id in uploaded_file_ids:
            try:
                client.files.delete(uploaded_file_id)
            except Exception as cleanup_error:
                logger.warning(
                    "%s could not delete uploaded file id %s: %s",
                    provider_name,
                    uploaded_file_id,
                    cleanup_error,
                )
                continue
