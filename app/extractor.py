from __future__ import annotations

import copy
import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from app.models import TextBlock
from app.utils import atomic_write_text


TRANSLATABLE_TAGS = {
    "p",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "caption",
    "figcaption",
    "td",
    "th",
    "dt",
    "dd",
}
TITLE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "code", "pre", "samp", "kbd", "math", "svg", "audio", "video", "img"}
PUNCT_OR_NUMBER_RE = re.compile(r"^[\W\d_]+$", re.UNICODE)


def parse_xhtml(path: Path) -> BeautifulSoup:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return BeautifulSoup(raw, "xml")


def extract_text_blocks(
    soup: BeautifulSoup,
    chapter_id: str,
    translate_titles: bool = True,
    translate_footnotes: bool = True,
) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for element in soup.find_all(TRANSLATABLE_TAGS):
        if not isinstance(element, Tag):
            continue
        tag_name = element.name.lower()
        if tag_name in TITLE_TAGS and not translate_titles:
            continue
        if not translate_footnotes and _looks_like_footnote(element):
            continue
        if _has_skip_ancestor(element):
            continue
        if element.find(SKIP_TAGS):
            continue
        if tag_name in TABLE_CELL_TAGS and _has_translatable_descendant(element):
            continue
        text = element.get_text(" ", strip=True)
        if not text or PUNCT_OR_NUMBER_RE.match(text):
            continue
        block_id = f"{chapter_id}_block_{len(blocks):04d}"
        blocks.append(TextBlock(block_id=block_id, tag=tag_name, text=text, element=element))
    return blocks


def apply_translations(
    soup: BeautifulSoup,
    blocks: list[TextBlock],
    translations: dict[str, str],
    mode: str,
    failed_block_ids: set[str] | None = None,
) -> None:
    failed_block_ids = failed_block_ids or set()
    table_blocks: dict[int, tuple[Tag, list[TextBlock]]] = {}
    for block in blocks:
        translation = translations.get(block.block_id)
        failed = block.block_id in failed_block_ids
        if not translation and not failed:
            continue
        element = block.element
        table = _nearest_table(element)
        if mode == "replace":
            if translation:
                _replace_element_text(element, translation)
            elif failed:
                _mark_failed_element(element)
            continue
        if table is not None:
            table_key = id(table)
            table_blocks.setdefault(table_key, (table, []))[1].append(block)
            continue

        translated = soup.new_tag(element.name)
        translated["class"] = "bilingual-translation translation-failed" if failed else "bilingual-translation"
        translated["data-source-block-id"] = block.block_id
        translated.string = translation or _failure_text()
        element.insert_after(translated)

    if mode != "replace":
        for table, table_block_list in table_blocks.values():
            _insert_translated_table_copy(table, table_block_list, translations, failed_block_ids)


def save_xhtml(soup: BeautifulSoup, path: Path) -> None:
    atomic_write_text(path, str(soup), encoding="utf-8")


def title_from_soup(soup: BeautifulSoup) -> str | None:
    title = soup.find("title")
    if title:
        text = title.get_text(" ", strip=True)
        if text:
            return text
    heading = soup.find(["h1", "h2", "h3"])
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return text
    return None


def inject_bilingual_style(path: Path) -> None:
    soup = parse_xhtml(path)
    if soup.find("style", id="bilingual-style"):
        return
    head = soup.find("head")
    if head is None:
        html = soup.find("html")
        head = soup.new_tag("head")
        if html:
            html.insert(0, head)
        else:
            soup.insert(0, head)
    style = soup.new_tag("style", id="bilingual-style", type="text/css")
    style.string = (
        ".bilingual-translation{margin-top:0.2em;margin-bottom:0.8em;opacity:0.85;}"
        ".translation-failed{color:#8a3a00;font-style:italic;}"
        ".bilingual-table-translation{margin-top:0.6em;margin-bottom:0.8em;opacity:0.92;"
        "page-break-inside:avoid;break-inside:avoid;}"
    )
    head.append(style)
    save_xhtml(soup, path)


def _has_skip_ancestor(element: Tag) -> bool:
    for parent in element.parents:
        if isinstance(parent, Tag) and parent.name and parent.name.lower() in SKIP_TAGS:
            return True
    return False


TABLE_CELL_TAGS = {"td", "th"}


def _has_translatable_descendant(element: Tag) -> bool:
    for descendant in element.find_all(TRANSLATABLE_TAGS):
        if isinstance(descendant, Tag) and descendant is not element:
            return True
    return False


def _nearest_table(element: Tag) -> Tag | None:
    for parent in [element, *element.parents]:
        if isinstance(parent, Tag) and parent.name and parent.name.lower() == "table":
            return parent
    return None


def _replace_element_text(element: Tag, translation: str) -> None:
    element.clear()
    element.string = translation


def _insert_translated_table_copy(
    table: Tag,
    blocks: list[TextBlock],
    translations: dict[str, str],
    failed_block_ids: set[str],
) -> None:
    marker_attr = "data-bilingual-temp-block-id"
    marked_elements: list[Tag] = []
    for block in blocks:
        translation = translations.get(block.block_id)
        failed = block.block_id in failed_block_ids
        if not translation and not failed:
            continue
        if not isinstance(block.element, Tag):
            continue
        block.element[marker_attr] = block.block_id
        marked_elements.append(block.element)

    translated_table = copy.deepcopy(table)
    for original in marked_elements:
        original.attrs.pop(marker_attr, None)

    _append_class(translated_table, "bilingual-table-translation")
    translated_table["data-source-table"] = "true"

    for block in blocks:
        translation = translations.get(block.block_id)
        failed = block.block_id in failed_block_ids
        if not translation and not failed:
            continue
        translated_element = translated_table.find(attrs={marker_attr: block.block_id})
        if isinstance(translated_element, Tag):
            translated_element.attrs.pop(marker_attr, None)
            if translation:
                _replace_element_text(translated_element, translation)
            elif failed:
                _replace_element_text(translated_element, f"{block.text} {_failure_text()}")
                _append_class(translated_element, "translation-failed")

    for leftover in translated_table.find_all(attrs={marker_attr: True}):
        if isinstance(leftover, Tag):
            leftover.attrs.pop(marker_attr, None)

    table.insert_after(translated_table)


def _append_class(element: Tag, class_name: str) -> None:
    existing = element.get("class")
    if existing is None:
        element["class"] = class_name
        return
    if isinstance(existing, list):
        classes = [str(item) for item in existing]
    else:
        classes = str(existing).split()
    if class_name not in classes:
        classes.append(class_name)
    element["class"] = " ".join(classes)


def _looks_like_footnote(element: Tag) -> bool:
    joined = " ".join(
        str(value).lower()
        for key, value in element.attrs.items()
        if key in {"id", "class", "epub:type", "role"}
    )
    return "footnote" in joined or "endnote" in joined


def _failure_text() -> str:
    return "[Translation failed after retries]"


def _mark_failed_element(element: Tag) -> None:
    _append_class(element, "translation-failed")
    existing_title = element.get("title")
    message = _failure_text()
    element["title"] = f"{existing_title} {message}".strip() if existing_title else message
    element["data-translation-status"] = "failed"
    if message not in element.get_text(" ", strip=True):
        element.append(f" {message}")
