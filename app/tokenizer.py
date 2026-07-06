from __future__ import annotations


try:
    import tiktoken
except ImportError:  # pragma: no cover - fallback for constrained installs
    tiktoken = None


_ENCODER = None


def count_tokens(text: str) -> int:
    global _ENCODER
    if not text:
        return 0
    if tiktoken is None:
        return max(1, len(text) // 3)
    if _ENCODER is None:
        try:
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = tiktoken.encoding_for_model("gpt-3.5-turbo")
    return len(_ENCODER.encode(text))
