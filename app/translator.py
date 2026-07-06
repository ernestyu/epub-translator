from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import Config
from app.llm_client import LLMClient
from app.models import TextBlock
from app.utils import atomic_write_json, now_ts, read_json

try:
    from json_repair import repair_json
except ImportError:  # pragma: no cover - exercised when optional dependency is missing
    repair_json = None


logger = logging.getLogger("epub-translator-web.translator")

SYSTEM_PROMPT = """You are a professional book translator.

Translate each input item into the target language.

Rules:
1. Return only valid JSON.
2. Do not use Markdown.
3. Do not wrap the answer in code fences.
4. Do not add explanations.
5. Preserve item ids exactly.
6. Return the same number of items as the input.
7. Do not omit any item.
8. Do not merge items.
9. Do not split items.
10. Translate only the text value.
11. Keep names, URLs, code identifiers, file paths, and commands unchanged unless they clearly need translation.
"""


class TranslationValidationError(ValueError):
    pass


class BatchTranslator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = LLMClient(config)
        self.cache_dir = config.cache_dir / "batches"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def translate_batch(
        self,
        blocks: list[TextBlock],
        target_language: str,
        mode: str,
        user_prompt: str | None,
        max_retries: int,
    ) -> dict[str, str]:
        input_items = [{"id": block.block_id, "text": block.text} for block in blocks]
        cache_path = self._cache_path(target_language, mode, user_prompt, input_items)
        cached = self._read_cache(cache_path, input_items)
        if cached is not None:
            return cached

        previous_error: str | None = None
        for attempt in range(1, max_retries + 1):
            messages = self._messages(target_language, input_items, user_prompt, previous_error)
            raw = self.client.chat(messages)
            try:
                translations = parse_and_validate(raw, input_items)
                atomic_write_json(
                    cache_path,
                    {
                        "created_at": now_ts(),
                        "model": self.config.llm_model,
                        "target_language": target_language,
                        "items": [
                            {"id": item_id, "translation": translations[item_id]}
                            for item_id in [item["id"] for item in input_items]
                        ],
                    },
                )
                return translations
            except TranslationValidationError as exc:
                previous_error = str(exc)
                logger.warning("Invalid LLM JSON attempt=%s reason=%s", attempt, previous_error)
        if len(blocks) > 1:
            logger.warning("Batch failed after retries; splitting into single-item fallback requests")
            translations: dict[str, str] = {}
            for block in blocks:
                translations.update(
                    self.translate_batch(
                        [block],
                        target_language=target_language,
                        mode=mode,
                        user_prompt=user_prompt,
                        max_retries=max_retries,
                    )
                )
            return translations
        raise TranslationValidationError(previous_error or "LLM response did not validate")

    def _messages(
        self,
        target_language: str,
        input_items: list[dict[str, str]],
        user_prompt: str | None,
        previous_error: str | None,
    ) -> list[dict[str, str]]:
        payload = {"target_language": target_language, "items": input_items}
        prompt_parts = [
            f"Target language: {target_language}",
            "",
            "Return JSON in this exact format:",
            '{"items":[{"id":"same id as input","translation":"translated text"}]}',
            "Escape all quotation marks and control characters inside translation strings.",
            "",
        ]
        if user_prompt:
            prompt_parts.extend(["Additional translation instructions:", user_prompt.strip(), ""])
        if previous_error:
            prompt_parts.extend(
                [
                    f"Your previous response was invalid because: {previous_error}.",
                    "Return only valid JSON with exactly the same ids.",
                    "",
                ]
            )
        prompt_parts.extend(["Input:", json.dumps(payload, ensure_ascii=False)])
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(prompt_parts)},
        ]

    def _cache_path(
        self,
        target_language: str,
        mode: str,
        user_prompt: str | None,
        input_items: list[dict[str, str]],
    ) -> Path:
        cache_input = {
            "model": self.config.llm_model,
            "target_language": target_language,
            "mode": mode,
            "user_prompt": user_prompt or "",
            "items": input_items,
        }
        digest = hashlib.sha256(json.dumps(cache_input, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, cache_path: Path, input_items: list[dict[str, str]]) -> dict[str, str] | None:
        if not cache_path.exists():
            return None
        try:
            payload = read_json(cache_path)
            raw = json.dumps({"items": payload.get("items", [])}, ensure_ascii=False)
            return parse_and_validate(raw, input_items)
        except Exception as exc:
            logger.warning("Ignoring invalid batch cache %s: %s", cache_path, exc)
            return None


def parse_and_validate(raw: str, input_items: list[dict[str, str]]) -> dict[str, str]:
    payload = _loads_with_light_repair(raw)
    if not isinstance(payload, dict):
        raise TranslationValidationError("top level response must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise TranslationValidationError("response.items must be a list")
    if len(items) != len(input_items):
        raise TranslationValidationError("response item count does not match input")

    expected_ids = [item["id"] for item in input_items]
    expected_set = set(expected_ids)
    seen: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise TranslationValidationError("each response item must be an object")
        item_id = item.get("id")
        translation = item.get("translation")
        if item_id not in expected_set:
            raise TranslationValidationError("response ids do not match input ids")
        if not isinstance(translation, str):
            raise TranslationValidationError("translation must be a string")
        source_text = next(input_item["text"] for input_item in input_items if input_item["id"] == item_id)
        if source_text and not translation.strip():
            raise TranslationValidationError("translation must not be empty")
        if _looks_like_xml_document(translation):
            raise TranslationValidationError("translation looks like XML or XHTML")
        if _looks_like_explanation(translation):
            raise TranslationValidationError("translation looks like explanatory text")
        seen[item_id] = translation.strip()
    if set(seen) != expected_set:
        raise TranslationValidationError("response ids do not match input ids")
    return {item_id: seen[item_id] for item_id in expected_ids}


def _loads_with_light_repair(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and start < end:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            repaired = _repair_json(candidate)
            if repaired is not None:
                return repaired
            raise TranslationValidationError(f"invalid JSON after light repair: {exc}") from exc
    raise TranslationValidationError("response is not valid JSON")


def _repair_json(candidate: str) -> Any | None:
    if repair_json is None:
        return None
    try:
        repaired = repair_json(candidate)
        if isinstance(repaired, str):
            return json.loads(repaired)
        return repaired
    except Exception:
        return None


def _looks_like_xml_document(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith("<?xml") or lowered.startswith("<html") or lowered.startswith("<body") or "</p>" in lowered


def _looks_like_explanation(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith("here is") or lowered.startswith("the translation") or lowered.startswith("i translated")
