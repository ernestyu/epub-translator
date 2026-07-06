from __future__ import annotations

from app.models import TextBlock
from app.tokenizer import count_tokens


def make_batches(
    blocks: list[TextBlock],
    max_items: int = 8,
    max_chars: int = 6000,
    max_tokens: int | None = None,
) -> list[list[TextBlock]]:
    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    batches: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    current_chars = 0
    current_tokens = 0

    for block in blocks:
        block_chars = len(block.text)
        block_tokens = count_tokens(block.text) + 24
        would_exceed_items = len(current) >= max_items
        would_exceed_chars = current and current_chars + block_chars > max_chars
        would_exceed_tokens = bool(max_tokens and current and current_tokens + block_tokens > max_tokens)
        if would_exceed_items or would_exceed_chars or would_exceed_tokens:
            batches.append(current)
            current = []
            current_chars = 0
            current_tokens = 0

        current.append(block)
        current_chars += block_chars
        current_tokens += block_tokens

        if block_chars >= max_chars or bool(max_tokens and block_tokens >= max_tokens):
            batches.append(current)
            current = []
            current_chars = 0
            current_tokens = 0

    if current:
        batches.append(current)
    return batches
