"""Server-sent events parsing for OpenAI-style streaming chat completions."""

from __future__ import annotations

import json


def sse_data(line: str) -> str | None:
    """Payload of an SSE "data:" line, or None for anything else.

    Blank lines, comments (": keep-alive") and other fields return None.
    """
    if not line.startswith("data:"):
        return None
    return line[5:].strip()


def chunk_has_output(payload: str) -> bool:
    """True if a chat.completion.chunk carries generated output.

    The first chunk vLLM sends usually holds only {"role": "assistant"}; that
    is not a token. Content, reasoning text or a tool call delta count.
    "[DONE]" and usage-only chunks do not.
    """
    if not payload or payload == "[DONE]":
        return False
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return False
    for choice in obj.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False
