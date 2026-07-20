from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GlossaryTerm:
    source: str
    target: str
    note: str = ""
    aliases: tuple[str, ...] = ()

    def to_prompt_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source, "target": self.target}
        if self.aliases:
            data["aliases"] = list(self.aliases)
        if self.note:
            data["note"] = self.note
        return data


def parse_glossary(text: str | None) -> list[GlossaryTerm]:
    if not text or not text.strip():
        return []
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return _parse_line_glossary(stripped)
    return _parse_json_glossary(payload)


def match_glossary_terms(block_texts: list[str], terms: list[GlossaryTerm]) -> list[GlossaryTerm]:
    if not terms:
        return []
    combined = "\n".join(block_texts)
    combined_lower = combined.lower()
    matched: list[GlossaryTerm] = []
    for term in terms:
        candidates = (term.source, *term.aliases)
        for candidate in candidates:
            if not candidate:
                continue
            if candidate in combined or candidate.lower() in combined_lower:
                matched.append(term)
                break
    return matched


def glossary_hash(terms: list[GlossaryTerm]) -> str:
    payload = [term.to_prompt_dict() for term in terms]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_json_glossary(payload: Any) -> list[GlossaryTerm]:
    if isinstance(payload, dict):
        if "terms" in payload:
            payload = payload["terms"]
        else:
            payload = [{"source": key, "target": value} for key, value in payload.items()]
    if not isinstance(payload, list):
        raise ValueError("Glossary JSON must be a list, an object, or an object with terms.")

    terms: list[GlossaryTerm] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target:
            continue
        aliases_value = item.get("aliases") or []
        if isinstance(aliases_value, str):
            aliases = tuple(part.strip() for part in aliases_value.split("|") if part.strip())
        elif isinstance(aliases_value, list):
            aliases = tuple(str(part).strip() for part in aliases_value if str(part).strip())
        else:
            aliases = ()
        terms.append(
            GlossaryTerm(
                source=source,
                target=target,
                note=str(item.get("note") or "").strip(),
                aliases=aliases,
            )
        )
    return terms


def _parse_line_glossary(text: str) -> list[GlossaryTerm]:
    terms: list[GlossaryTerm] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"\s*(?:=>|->|\t|,)\s*", line, maxsplit=2)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        note = parts[2] if len(parts) > 2 else ""
        terms.append(GlossaryTerm(source=parts[0], target=parts[1], note=note))
    return terms
