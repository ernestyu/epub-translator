from __future__ import annotations

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
        text = element.get_text(" ", strip=True)
        if not text or PUNCT_OR_NUMBER_RE.match(text):
            continue
        block_id = f"{chapter_id}_block_{len(blocks):04d}"
        blocks.append(TextBlock(block_id=block_id, tag=tag_name, text=text, element=element))
    return blocks


def apply_translations(soup: BeautifulSoup, blocks: list[TextBlock], translations: dict[str, str], mode: str) -> None:
    for block in blocks:
        translation = translations.get(block.block_id)
        if not translation:
            continue
        element = block.element
        if mode == "replace":
            element.clear()
            element.string = translation
            continue

        translated = soup.new_tag(element.name)
        translated["class"] = "bilingual-translation"
        translated["data-source-block-id"] = block.block_id
        translated.string = translation
        element.insert_after(translated)


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
    style.string = ".bilingual-translation{margin-top:0.2em;margin-bottom:0.8em;opacity:0.85;}"
    head.append(style)
    save_xhtml(soup, path)


def _has_skip_ancestor(element: Tag) -> bool:
    for parent in element.parents:
        if isinstance(parent, Tag) and parent.name and parent.name.lower() in SKIP_TAGS:
            return True
    return False


def _looks_like_footnote(element: Tag) -> bool:
    joined = " ".join(
        str(value).lower()
        for key, value in element.attrs.items()
        if key in {"id", "class", "epub:type", "role"}
    )
    return "footnote" in joined or "endnote" in joined
