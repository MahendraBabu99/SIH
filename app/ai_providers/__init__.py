"""Multi-provider AI abstraction layer for AIFT forensic analysis.

This package provides a unified interface for interacting with multiple AI
providers used in AI Forensic Triage (AIFT) analysis workflows. It abstracts
away provider-specific SDK differences behind a common ``AIProvider`` base
class, enabling the rest of the application to perform AI-powered forensic
analysis without coupling to any single vendor.

Supported providers:

* **Claude (Anthropic)** -- via the ``anthropic`` Python SDK.
* **OpenAI** -- via the ``openai`` Python SDK (Chat Completions and
  Responses APIs).
* **Moonshot Kimi** -- via the ``openai`` Python SDK pointed at the
  Moonshot API base URL.
* **OpenAI-compatible local endpoints** -- Ollama, LM Studio, vLLM, or
  any server exposing an OpenAI-compatible ``/v1/chat/completions``
  endpoint.
"""

from __future__ import annotations

from .base import (
    AIProvider,
    AIProviderError,
    DEFAULT_KIMI_BASE_URL,
    DEFAULT_KIMI_FILE_UPLOAD_PURPOSE,
    DEFAULT_KIMI_MODEL,
)
from .claude_provider import ClaudeProvider
from .factory import create_provider
from .kimi_provider import KimiProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider
from .utils import (
    StreamedResponseChunk,
    stream_chunk_answer_text,
    stream_chunk_reasoning_text,
)

__all__ = [
    "AIProvider",
    "AIProviderError",
    "ClaudeProvider",
    "OpenAIProvider",
    "KimiProvider",
    "LocalProvider",
    "StreamedResponseChunk",
    "create_provider",
    "stream_chunk_answer_text",
    "stream_chunk_reasoning_text",
]
