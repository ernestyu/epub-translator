from __future__ import annotations

import base64
import html
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.batcher import make_batches
from app.config import Config
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import apply_translations, extract_text_blocks, inject_bilingual_style, parse_xhtml, save_xhtml, title_from_soup
from app.glossary import match_glossary_terms, parse_glossary
from app.i18n import translate
from app.models import TextBlock
from app.packager import _write_epub
from app.translator import BatchTranslator, PartialTranslationError
from app.worker import _neighbor_context


@dataclass(frozen=True)
class PreviewChapter:
    index: int
    title: str
    href: str
    text_blocks: int
    chars: int

    @property
    def label(self) -> str:
        clean_title = self.title or "(untitled)"
        return f"{self.index} | {clean_title} | {self.chars} chars | {self.href}"


def epub_reader_html(epub_path: Path, title: str = "EPUB Preview", ui_language: str = "zh") -> str:
    encoded = base64.b64encode(epub_path.read_bytes()).decode("ascii")
    inner = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/epubjs@0.3.93/dist/epub.min.js"></script>
  <style>
    html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; background: #f6f6f3; }}
    .bar {{ height: 44px; display: flex; align-items: center; gap: 8px; padding: 0 10px; border-bottom: 1px solid #ddd; background: #fff; }}
    button {{ border: 1px solid #bbb; background: #fff; padding: 5px 10px; border-radius: 4px; cursor: pointer; }}
    #viewer {{ height: calc(100% - 45px); max-width: 960px; margin: 0 auto; background: #fff; box-shadow: 0 0 0 1px #ddd; }}
    #status {{ margin-left: auto; font-size: 12px; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  </style>
</head>
<body>
  <div class="bar">
    <button id="prev" title="{html.escape(translate(ui_language, "reader_prev"))}">{html.escape(translate(ui_language, "reader_prev"))}</button>
    <button id="next" title="{html.escape(translate(ui_language, "reader_next"))}">{html.escape(translate(ui_language, "reader_next"))}</button>
    <span>{html.escape(title)}</span>
    <span id="status">Loading...</span>
  </div>
  <div id="viewer"></div>
  <script>
    function b64ToArrayBuffer(b64) {{
      const binary = atob(b64);
      const len = binary.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
      return bytes.buffer;
    }}
    const status = document.getElementById("status");
    status.textContent = "{html.escape(translate(ui_language, "reader_loading"))}";
    try {{
      const book = ePub(b64ToArrayBuffer("{encoded}"));
      const rendition = book.renderTo("viewer", {{ width: "100%", height: "100%", spread: "none" }});
      rendition.display();
      document.getElementById("prev").onclick = () => rendition.prev();
      document.getElementById("next").onclick = () => rendition.next();
      rendition.on("relocated", location => {{
        status.textContent = location && location.start ? location.start.href : "";
      }});
      book.ready.then(() => {{ status.textContent = "{html.escape(translate(ui_language, "reader_ready"))}"; }});
    }} catch (error) {{
      status.textContent = "{html.escape(translate(ui_language, "reader_failed"))}: " + error;
    }}
  </script>
</body>
</html>"""
    return (
        "<iframe "
        "sandbox=\"allow-scripts allow-same-origin\" "
        "style=\"width:100%;height:760px;border:1px solid #ccc;border-radius:6px;background:#fff\" "
        f"srcdoc=\"{html.escape(inner, quote=True)}\">"
        "</iframe>"
    )


def chapter_summaries(source_path: Path, translate_titles: bool, translate_footnotes: bool, config: Config) -> list[PreviewChapter]:
    with tempfile.TemporaryDirectory(dir=config.cache_dir) as tmp:
        work_dir = Path(tmp) / "work"
        unpack_epub(source_path, work_dir)
        epub_info = read_epub_info(work_dir)
        chapters: list[PreviewChapter] = []
        for chapter in epub_info.chapters:
            soup = parse_xhtml(chapter.abs_path)
            chapter_id = f"chapter_{chapter.index:03d}"
            blocks = extract_text_blocks(
                soup,
                chapter_id,
                translate_titles=translate_titles,
                translate_footnotes=translate_footnotes,
            )
            chapters.append(
                PreviewChapter(
                    index=chapter.index,
                    title=title_from_soup(soup) or "",
                    href=chapter.href,
                    text_blocks=len(blocks),
                    chars=sum(len(block.text) for block in blocks),
                )
            )
        return chapters


def build_translated_preview_epub(
    source_path: Path,
    chapter_index: int,
    start_char: int,
    char_count: int,
    target_language: str,
    mode: str,
    batch_size: int,
    max_batch_chars: int,
    max_retries: int,
    user_prompt: str | None,
    translate_titles: bool,
    translate_footnotes: bool,
    config: Config,
    glossary_text: str | None = None,
) -> Path:
    preview_dir = config.cache_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.cache_dir) as tmp:
        tmp_path = Path(tmp)
        work_dir = tmp_path / "work"
        unpack_epub(source_path, work_dir)
        epub_info = read_epub_info(work_dir)
        if chapter_index < 0 or chapter_index >= len(epub_info.chapters):
            raise ValueError("Selected chapter does not exist")

        chapter = epub_info.chapters[chapter_index]
        soup = parse_xhtml(chapter.abs_path)
        chapter_id = f"chapter_{chapter.index:03d}"
        blocks = extract_text_blocks(
            soup,
            chapter_id,
            translate_titles=translate_titles,
            translate_footnotes=translate_footnotes,
        )
        selected = select_blocks_by_char_range(blocks, start_char=start_char, char_count=char_count)
        if not selected:
            raise ValueError("Selected range has no translatable text")

        translator = BatchTranslator(config)
        translations: dict[str, str] = {}
        failed_block_ids: set[str] = set()
        glossary_terms = parse_glossary(glossary_text)
        for batch in make_batches(
            selected,
            max_items=batch_size,
            max_chars=max_batch_chars,
            max_tokens=config.llm_max_input_tokens,
        ):
            previous_context, next_context = _neighbor_context(blocks, batch)
            matched_glossary = match_glossary_terms([block.text for block in batch], glossary_terms)
            try:
                translations.update(
                    translator.translate_batch(
                        batch,
                        target_language=target_language,
                        mode=mode,
                        user_prompt=user_prompt,
                        max_retries=max_retries,
                        previous_context=previous_context,
                        next_context=next_context,
                        glossary_terms=matched_glossary,
                    )
                )
            except PartialTranslationError as exc:
                translations.update(exc.translations)
                failed_block_ids.update(block_id for block_id in exc.failed_ids if block_id not in exc.translations)

        apply_translations(soup, selected, translations, mode, failed_block_ids=failed_block_ids)
        save_xhtml(soup, chapter.abs_path)
        inject_bilingual_style(chapter.abs_path)
        output_path = preview_dir / f"preview-{next(tempfile._get_candidate_names())}.epub"
        _write_epub(work_dir, output_path)
        return output_path


def select_blocks_by_char_range(blocks: list[TextBlock], start_char: int, char_count: int) -> list[TextBlock]:
    start = max(0, start_char)
    end = start + max(1, char_count)
    selected: list[TextBlock] = []
    cursor = 0
    for block in blocks:
        block_start = cursor
        block_end = cursor + len(block.text)
        if block_end > start and block_start < end:
            selected.append(block)
        cursor = block_end
    return selected
