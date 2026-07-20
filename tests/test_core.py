from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.batcher import make_batches
from app.config import Config
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import apply_translations, extract_text_blocks, parse_xhtml, save_xhtml
from app.job_store import JobStore
from app.models import TextBlock
from app.packager import _write_epub
from app.preview import select_blocks_by_char_range
from app.glossary import match_glossary_terms, parse_glossary
from app.translator import BatchTranslator, PartialTranslationError, TranslationValidationError, parse_and_validate, parse_and_validate_partial
import app.worker as worker_module


class CoreTests(unittest.TestCase):
    def test_batcher_respects_item_and_character_limits(self) -> None:
        blocks = [
            TextBlock(block_id="a", tag="p", text="a" * 3),
            TextBlock(block_id="b", tag="p", text="b" * 3),
            TextBlock(block_id="c", tag="p", text="c" * 10),
        ]
        batches = make_batches(blocks, max_items=2, max_chars=6)
        self.assertEqual([[block.block_id for block in batch] for batch in batches], [["a", "b"], ["c"]])

    def test_batcher_respects_token_budget(self) -> None:
        blocks = [
            TextBlock(block_id="a", tag="p", text="hello " * 80),
            TextBlock(block_id="b", tag="p", text="world " * 80),
        ]
        batches = make_batches(blocks, max_items=8, max_chars=10000, max_tokens=90)
        self.assertEqual([[block.block_id for block in batch] for batch in batches], [["a"], ["b"]])

    def test_select_blocks_by_char_range_keeps_whole_overlapping_blocks(self) -> None:
        blocks = [
            TextBlock(block_id="a", tag="p", text="abcde"),
            TextBlock(block_id="b", tag="p", text="fghij"),
            TextBlock(block_id="c", tag="p", text="klmno"),
        ]
        selected = select_blocks_by_char_range(blocks, start_char=4, char_count=4)
        self.assertEqual([block.block_id for block in selected], ["a", "b"])

    def test_parse_and_validate_repairs_code_fence_and_reorders_by_id(self) -> None:
        raw = """```json
{"items":[{"id":"b","translation":"二"},{"id":"a","translation":"一"}]}
```"""
        result = parse_and_validate(raw, [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
        self.assertEqual(result, {"a": "一", "b": "二"})

    def test_parse_and_validate_rejects_length_mismatch(self) -> None:
        with self.assertRaises(TranslationValidationError):
            parse_and_validate(json.dumps({"items": []}), [{"id": "a", "text": "one"}])

    def test_partial_validation_keeps_successful_items_and_reports_missing(self) -> None:
        result = parse_and_validate_partial(
            json.dumps({"items": [{"id": "a", "translation": "一"}]}),
            [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}],
        )
        self.assertEqual(result.translations, {"a": "一"})
        self.assertEqual(result.failed_ids, ["b"])

    def test_parse_and_validate_repairs_missing_comma_when_possible(self) -> None:
        raw = '{"items":[{"id":"a","translation":"一"} {"id":"b","translation":"二"}]}'
        result = parse_and_validate(raw, [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
        self.assertEqual(result, {"a": "一", "b": "二"})

    def test_batch_translator_retries_only_missing_items_after_partial_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _test_config(Path(tmp) / "data")
            translator = BatchTranslator(config)
            translator.client = SequencedClient(
                [
                    json.dumps({"items": [{"id": "a", "translation": "T:one"}]}),
                    json.dumps({"items": [{"id": "b", "translation": "T:two"}]}),
                ]
            )
            blocks = [
                TextBlock(block_id="a", tag="p", text="one"),
                TextBlock(block_id="b", tag="p", text="two"),
            ]

            result = translator.translate_batch(
                blocks,
                target_language="Simplified Chinese",
                mode="append_block",
                user_prompt=None,
                max_retries=3,
            )

            self.assertEqual(result, {"a": "T:one", "b": "T:two"})
            self.assertEqual(len(translator.client.calls), 2)
            self.assertIn('"id": "b"', translator.client.calls[1])
            self.assertNotIn('"id": "a"', translator.client.calls[1])

    def test_epub_unpack_spine_extract_insert_and_repackage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "book.epub"
            work = tmp_path / "work"
            repacked = tmp_path / "repacked.epub"
            _create_sample_epub(source)

            unpack_epub(source, work)
            info = read_epub_info(work)
            self.assertEqual(len(info.chapters), 2)
            self.assertEqual(info.chapters[0].href, "Text/ch1.xhtml")

            soup = parse_xhtml(info.chapters[0].abs_path)
            blocks = extract_text_blocks(soup, "chapter_000")
            self.assertEqual([block.tag for block in blocks], ["h1", "p", "li"])
            translations = {block.block_id: f"T:{block.text}" for block in blocks}
            apply_translations(soup, blocks, translations, "append_block")
            save_xhtml(soup, info.chapters[0].abs_path)
            self.assertIn("bilingual-translation", info.chapters[0].abs_path.read_text(encoding="utf-8"))

            _write_epub(work, repacked)
            with zipfile.ZipFile(repacked) as zf:
                self.assertEqual(zf.namelist()[0], "mimetype")
                self.assertEqual(zf.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)

    def test_append_block_keeps_original_table_and_adds_translated_table_copy(self) -> None:
        soup = _parse_xhtml_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
  <table class="facts">
    <tr><th>Name</th><th>Role</th></tr>
    <tr><td>Alpha</td><td>First item</td></tr>
  </table>
</body>
</html>"""
        )
        blocks = extract_text_blocks(soup, "chapter_000")
        self.assertEqual([block.tag for block in blocks], ["th", "th", "td", "td"])

        translations = {block.block_id: f"T:{block.text}" for block in blocks}
        apply_translations(soup, blocks, translations, "append_block")

        tables = soup.find_all("table")
        self.assertEqual(len(tables), 2)
        original, translated = tables
        self.assertEqual([cell.get_text(" ", strip=True) for cell in original.find_all(["th", "td"])], ["Name", "Role", "Alpha", "First item"])
        self.assertEqual(
            [cell.get_text(" ", strip=True) for cell in translated.find_all(["th", "td"])],
            ["T:Name", "T:Role", "T:Alpha", "T:First item"],
        )
        self.assertEqual(len(original.find_all("td")), 2)
        self.assertEqual(len(translated.find_all("td")), 2)
        self.assertIn("bilingual-table-translation", str(translated.get("class")))

    def test_replace_mode_updates_table_cells_without_copying_table(self) -> None:
        soup = _parse_xhtml_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><table><tr><td>Alpha</td><td>Beta</td></tr></table></body>
</html>"""
        )
        blocks = extract_text_blocks(soup, "chapter_000")
        translations = {block.block_id: f"T:{block.text}" for block in blocks}

        apply_translations(soup, blocks, translations, "replace")

        tables = soup.find_all("table")
        self.assertEqual(len(tables), 1)
        self.assertEqual([cell.get_text(" ", strip=True) for cell in tables[0].find_all("td")], ["T:Alpha", "T:Beta"])

    def test_apply_translations_marks_failed_blocks_without_dropping_original(self) -> None:
        soup = _parse_xhtml_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Needs translation.</p></body></html>"""
        )
        blocks = extract_text_blocks(soup, "chapter_000")
        apply_translations(soup, blocks, {}, "append_block", failed_block_ids={blocks[0].block_id})

        text = soup.get_text(" ", strip=True)
        self.assertIn("Needs translation.", text)
        self.assertIn("Translation failed after retries", text)

    def test_table_cells_with_paragraphs_do_not_translate_parent_cell_twice(self) -> None:
        soup = _parse_xhtml_text(
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><table><tr><td><p>Nested text</p></td><td>Plain text</td></tr></table></body>
</html>"""
        )
        blocks = extract_text_blocks(soup, "chapter_000")
        self.assertEqual([block.tag for block in blocks], ["p", "td"])

    def test_worker_finishes_job_with_mock_translator_and_writes_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "book.epub"
            _create_sample_epub(source)
            config = _test_config(tmp_path / "data")
            store = JobStore(config)
            job = store.create_job(
                uploaded_path=source,
                original_filename="book.epub",
                source_language="English",
                target_language="Simplified Chinese",
                mode="append_block",
                batch_size=2,
                max_batch_chars=6000,
                max_batch_retries=3,
                chapter_failure_policy="stop_on_failed_chapter",
                user_prompt=None,
                translate_titles=True,
                translate_footnotes=True,
                translate_toc=False,
            )

            original_translator = worker_module.BatchTranslator
            worker_module.BatchTranslator = FakeBatchTranslator
            try:
                worker_module.run_job(config, store, job.job_id)
            finally:
                worker_module.BatchTranslator = original_translator

            finished = store.load(job.job_id)
            self.assertEqual(finished.status, "finished")
            self.assertTrue(Path(finished.output_path).exists())
            self.assertTrue(Path(finished.chapters[0].translated_path).exists())
            self.assertIn(
                "bilingual-translation",
                Path(finished.chapters[0].translated_path).read_text(encoding="utf-8"),
            )

    def test_worker_can_translate_only_selected_block_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "book.epub"
            _create_sample_epub(source)
            config = _test_config(tmp_path / "data")
            store = JobStore(config)
            job = store.create_job(
                uploaded_path=source,
                original_filename="book.epub",
                source_language="English",
                target_language="Simplified Chinese",
                mode="append_block",
                batch_size=2,
                max_batch_chars=6000,
                max_batch_retries=3,
                chapter_failure_policy="stop_on_failed_chapter",
                user_prompt=None,
                translate_titles=True,
                translate_footnotes=True,
                translate_toc=False,
                translate_start_block=2,
                translate_end_block=2,
            )

            original_translator = worker_module.BatchTranslator
            worker_module.BatchTranslator = FakeBatchTranslator
            try:
                worker_module.run_job(config, store, job.job_id)
            finally:
                worker_module.BatchTranslator = original_translator

            finished = store.load(job.job_id)
            self.assertEqual(finished.status, "finished")
            first_chapter = Path(finished.chapters[0].translated_path).read_text(encoding="utf-8")
            self.assertIn("T:Hello world.", first_chapter)
            self.assertNotIn("T:Chapter One", first_chapter)
            self.assertNotIn("T:First item.", first_chapter)

    def test_worker_keeps_failed_unit_with_warning_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "book.epub"
            _create_sample_epub(source)
            config = _test_config(tmp_path / "data")
            store = JobStore(config)
            job = store.create_job(
                uploaded_path=source,
                original_filename="book.epub",
                source_language="English",
                target_language="Simplified Chinese",
                mode="append_block",
                batch_size=2,
                max_batch_chars=6000,
                max_batch_retries=1,
                chapter_failure_policy="keep_original_on_failed_chapter",
                user_prompt=None,
                translate_titles=True,
                translate_footnotes=True,
                translate_toc=False,
            )

            original_translator = worker_module.BatchTranslator
            worker_module.BatchTranslator = PartiallyFailingBatchTranslator
            try:
                worker_module.run_job(config, store, job.job_id)
            finally:
                worker_module.BatchTranslator = original_translator

            finished = store.load(job.job_id)
            self.assertEqual(finished.status, "finished_with_warnings")
            self.assertGreater(finished.failed_text_blocks, 0)
            first_chapter = Path(finished.chapters[0].translated_path).read_text(encoding="utf-8")
            self.assertIn("Translation failed after retries", first_chapter)

    def test_glossary_parser_and_matcher(self) -> None:
        terms = parse_glossary("Wallfacer => 面壁者\nTrisolaran => 三体人")
        matched = match_glossary_terms(["The Wallfacer spoke."], terms)
        self.assertEqual([term.source for term in matched], ["Wallfacer"])


class FakeBatchTranslator:
    def __init__(self, config: Config) -> None:
        self.config = config

    def translate_batch(self, blocks, target_language, mode, user_prompt, max_retries, **kwargs):
        return {block.block_id: f"T:{block.text}" for block in blocks}


class PartiallyFailingBatchTranslator:
    def __init__(self, config: Config) -> None:
        self.config = config

    def translate_batch(self, blocks, target_language, mode, user_prompt, max_retries, **kwargs):
        translations = {
            block.block_id: f"T:{block.text}"
            for block in blocks
            if "Hello world" not in block.text
        }
        failed_ids = [block.block_id for block in blocks if "Hello world" in block.text]
        if failed_ids:
            raise PartialTranslationError("mock failure", translations, failed_ids)
        return translations


class SequencedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def chat(self, messages):
        self.calls.append(messages[-1]["content"])
        return self.responses.pop(0)


def _create_sample_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <manifest>
    <item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="Styles/main.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>""",
        )
        zf.writestr(
            "OEBPS/Text/ch1.xhtml",
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title></head>
<body><h1>Chapter One</h1><p>Hello world.</p><ul><li>First item.</li></ul><pre>skip me</pre></body>
</html>""",
        )
        zf.writestr(
            "OEBPS/Text/ch2.xhtml",
            """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Two</title></head><body><p>Second.</p></body></html>""",
        )
        zf.writestr("OEBPS/Styles/main.css", "body { margin: 1em; }")


def _parse_xhtml_text(content: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.xhtml"
        path.write_text(content, encoding="utf-8")
        return parse_xhtml(path)


def _test_config(data_dir: Path) -> Config:
    return Config(
        data_dir=data_dir,
        jobs_dir=data_dir / "jobs",
        output_dir=data_dir / "output",
        cache_dir=data_dir / "cache",
        logs_dir=data_dir / "logs",
        app_host="127.0.0.1",
        app_port=7860,
        llm_base_url="http://example.invalid/v1",
        llm_api_key="test",
        llm_model="test-model",
        llm_context_window=8192,
        llm_timeout_seconds=1,
        llm_temperature=0.1,
        llm_top_p=0.8,
        default_source_language="English",
        default_target_language="Simplified Chinese",
        default_output_mode="append_block",
        default_batch_size=2,
        default_max_batch_chars=6000,
        default_batch_retries=3,
        default_chapter_failure_policy="stop_on_failed_chapter",
        default_translate_titles=True,
        default_translate_footnotes=True,
        ui_language="zh",
        log_level="INFO",
    )


if __name__ == "__main__":
    unittest.main()
