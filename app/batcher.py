from __future__ import annotations

from app.models import TextBlock


def make_batches(
    blocks: list[TextBlock], max_items: int = 8, max_chars: int = 6000
) -> list[list[TextBlock]]:
    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    batches: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    current_chars = 0

    for block in blocks:
        block_chars = len(block.text)
        would_exceed_items = len(current) >= max_items
        would_exceed_chars = current and current_chars + block_chars > max_chars
        if would_exceed_items or would_exceed_chars:
            batches.append(current)
            current = []
            current_chars = 0

        current.append(block)
        current_chars += block_chars

        if block_chars >= max_chars:
            batches.append(current)
            current = []
            current_chars = 0

    if current:
        batches.append(current)
    return batches
