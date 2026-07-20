from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from app.config import Config
from app.glossary import GlossaryTerm, glossary_hash
from app.llm_client import LLMClient
from app.models import TextBlock
from app.tokenizer import count_tokens
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
12. Previous and next context are reference only; do not translate or output them.
13. Use the glossary terms whenever they appear in the text.
"""


class TranslationValidationError(ValueError):
    pass


class PartialTranslationError(TranslationValidationError):
    def __init__(self, message: str, translations: dict[str, str], failed_ids: list[str]) -> None:
        super().__init__(message)
        self.translations = translations
        self.failed_ids = failed_ids


class ValidationResult:
    def __init__(
        self,
        translations: dict[str, str],
        failed_ids: list[str],
        errors: list[str],
    ) -> None:
        self.translations = translations
        self.failed_ids = failed_ids
        self.errors = errors

    @property
    def ok(self) -> bool:
        return not self.failed_ids and not self.errors


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
        previous_context: str | None = None,
        next_context: str | None = None,
        glossary_terms: list[GlossaryTerm] | None = None,
    ) -> dict[str, str]:
        input_items = [{"id": block.block_id, "text": block.text} for block in blocks]
        glossary_terms = glossary_terms or []
        cache_path = self._cache_path(
            target_language,
            mode,
            user_prompt,
            input_items,
            previous_context=previous_context,
            next_context=next_context,
            glossary_terms=glossary_terms,
        )
        cached = self._read_cache(cache_path, input_items)
        if cached is not None:
            logger.info("Batch cache hit items=%s estimated_input_tokens=%s", len(blocks), _estimated_input_tokens(input_items))
            return cached

        previous_error: str | None = None
        translations: dict[str, str] = {}
        pending_blocks = list(blocks)
        for attempt in range(1, max_retries + 1):
            pending_items = [{"id": block.block_id, "text": block.text} for block in pending_blocks]
            messages = self._messages(
                target_language,
                pending_items,
                user_prompt,
                previous_error,
                previous_context=previous_context,
                next_context=next_context,
                glossary_terms=glossary_terms,
            )
            started = time.perf_counter()
            raw = self.client.chat(messages)
            try:
                result = parse_and_validate_partial(raw, pending_items)
                translations.update(result.translations)
                if result.ok:
                    elapsed = time.perf_counter() - started
                    logger.info(
                        "Batch translated items=%s estimated_input_tokens=%s elapsed=%.2fs attempt=%s",
                        len(blocks),
                        _estimated_input_tokens(input_items),
                        elapsed,
                        attempt,
                    )
                    self._write_cache(cache_path, target_language, input_items, translations)
                    return {item["id"]: translations[item["id"]] for item in input_items}
                previous_error = "; ".join(result.errors) if result.errors else f"missing ids: {', '.join(result.failed_ids)}"
                pending_blocks = [block for block in pending_blocks if block.block_id in set(result.failed_ids)]
                if not pending_blocks:
                    self._write_cache(cache_path, target_language, input_items, translations)
                    return {item["id"]: translations[item["id"]] for item in input_items}
                logger.warning(
                    "Partial LLM result attempt=%s translated=%s failed=%s reason=%s",
                    attempt,
                    len(result.translations),
                    len(result.failed_ids),
                    previous_error,
                )
            except TranslationValidationError as exc:
                previous_error = str(exc)
                logger.warning("Invalid LLM JSON attempt=%s reason=%s", attempt, previous_error)

        if pending_blocks and len(pending_blocks) > 1:
            logger.warning("Batch failed after retries; splitting items=%s", len(pending_blocks))
            failed_ids: list[str] = []
            mid = max(1, len(pending_blocks) // 2)
            for subset in (pending_blocks[:mid], pending_blocks[mid:]):
                if not subset:
                    continue
                try:
                    translations.update(
                        self.translate_batch(
                            subset,
                            target_language=target_language,
                            mode=mode,
                            user_prompt=user_prompt,
                            max_retries=max_retries,
                            previous_context=previous_context,
                            next_context=next_context,
                            glossary_terms=glossary_terms,
                        )
                    )
                except PartialTranslationError as exc:
                    translations.update(exc.translations)
                    failed_ids.extend(exc.failed_ids)
                except TranslationValidationError:
                    failed_ids.extend(block.block_id for block in subset)
            if failed_ids:
                raise PartialTranslationError(
                    previous_error or "LLM response did not validate after split retries",
                    translations,
                    failed_ids,
                )
            self._write_cache(cache_path, target_language, input_items, translations)
            return {item["id"]: translations[item["id"]] for item in input_items}

        if pending_blocks:
            failed_ids = [block.block_id for block in pending_blocks]
            raise PartialTranslationError(previous_error or "LLM response did not validate", translations, failed_ids)

        if set(translations) == {item["id"] for item in input_items}:
            self._write_cache(cache_path, target_language, input_items, translations)
            return {item["id"]: translations[item["id"]] for item in input_items}

        missing = [item["id"] for item in input_items if item["id"] not in translations]
        raise PartialTranslationError(previous_error or "LLM response did not validate", translations, missing)

    def _write_cache(
        self,
        cache_path: Path,
        target_language: str,
        input_items: list[dict[str, str]],
        translations: dict[str, str],
    ) -> None:
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

    def _messages(
        self,
        target_language: str,
        input_items: list[dict[str, str]],
        user_prompt: str | None,
        previous_error: str | None,
        previous_context: str | None = None,
        next_context: str | None = None,
        glossary_terms: list[GlossaryTerm] | None = None,
    ) -> list[dict[str, str]]:
        glossary_terms = glossary_terms or []
        payload: dict[str, Any] = {"target_language": target_language, "items": input_items}
        if previous_context:
            payload["previous_context"] = previous_context
        if next_context:
            payload["next_context"] = next_context
        if glossary_terms:
            payload["glossary"] = [term.to_prompt_dict() for term in glossary_terms]
        prompt_parts = [
            f"Target language: {target_language}",
            "",
            "Return JSON in this exact format:",
            '{"items":[{"id":"same id as input","translation":"translated text"}]}',
            "Escape all quotation marks and control characters inside translation strings.",
            "Previous and next context are reference only. Do not translate or repeat context.",
            "Only translate items listed in payload.items.",
            "",
        ]
        if glossary_terms:
            prompt_parts.extend(
                [
                    "Glossary terms are mandatory when they appear in the text:",
                    json.dumps([term.to_prompt_dict() for term in glossary_terms], ensure_ascii=False),
                    "",
                ]
            )
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
        previous_context: str | None = None,
        next_context: str | None = None,
        glossary_terms: list[GlossaryTerm] | None = None,
    ) -> Path:
        cache_input = {
            "model": self.config.llm_model,
            "target_language": target_language,
            "mode": mode,
            "user_prompt": user_prompt or "",
            "previous_context": previous_context or "",
            "next_context": next_context or "",
            "glossary_hash": glossary_hash(glossary_terms or []),
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
    result = parse_and_validate_partial(raw, input_items)
    if not result.ok:
        reason = "; ".join(result.errors) if result.errors else f"failed ids: {', '.join(result.failed_ids)}"
        raise TranslationValidationError(reason)
    return {item["id"]: result.translations[item["id"]] for item in input_items}


def parse_and_validate_partial(raw: str, input_items: list[dict[str, str]]) -> ValidationResult:
    payload = _loads_with_light_repair(raw)
    if not isinstance(payload, dict):
        raise TranslationValidationError("top level response must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise TranslationValidationError("response.items must be a list")

    expected_ids = [item["id"] for item in input_items]
    expected_set = set(expected_ids)
    source_by_id = {item["id"]: item["text"] for item in input_items}
    seen: dict[str, str] = {}
    invalid_ids: set[str] = set()
    errors: list[str] = []
    duplicate_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("each response item must be an object")
            continue
        item_id = item.get("id")
        translation = item.get("translation")
        if item_id not in expected_set:
            errors.append(f"extra id: {item_id}")
            continue
        if item_id in seen:
            duplicate_ids.add(str(item_id))
            invalid_ids.add(str(item_id))
            continue
        if not isinstance(translation, str):
            errors.append(f"translation must be a string: {item_id}")
            invalid_ids.add(str(item_id))
            continue
        source_text = source_by_id[str(item_id)]
        if source_text and not translation.strip():
            errors.append(f"translation must not be empty: {item_id}")
            invalid_ids.add(str(item_id))
            continue
        if _looks_like_xml_document(translation):
            errors.append(f"translation looks like XML or XHTML: {item_id}")
            invalid_ids.add(str(item_id))
            continue
        if _looks_like_explanation(translation):
            errors.append(f"translation looks like explanatory text: {item_id}")
            invalid_ids.add(str(item_id))
            continue
        seen[str(item_id)] = translation.strip()
    for item_id in duplicate_ids:
        seen.pop(item_id, None)
        errors.append(f"duplicate id: {item_id}")
    missing_ids = expected_set - set(seen)
    failed_ids = sorted(missing_ids | invalid_ids, key=expected_ids.index)
    return ValidationResult(
        translations={item_id: seen[item_id] for item_id in expected_ids if item_id in seen},
        failed_ids=failed_ids,
        errors=errors,
    )


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


def _estimated_input_tokens(input_items: list[dict[str, str]]) -> int:
    return sum(count_tokens(item["text"]) + 24 for item in input_items)
