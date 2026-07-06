from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.batcher import make_batches
from app.config import Config
from app.extractor import apply_translations, extract_text_blocks, parse_xhtml, save_xhtml
from app.job_store import JobStore
from app.models import JobState
from app.packager import package_job
from app.translator import BatchTranslator
from app.utils import atomic_write_json, now_ts


logger = logging.getLogger("epub-translator-web.worker")


class JobCancelled(RuntimeError):
    pass


class WorkerManager:
    def __init__(self, config: Config, store: JobStore) -> None:
        self.config = config
        self.store = store
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, job_id: str, failed_only: bool = False) -> str:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return "已有任务正在后台运行，当前第一版仅支持单任务执行。"
            self._thread = threading.Thread(
                target=self._run_guarded,
                args=(job_id, failed_only),
                daemon=True,
                name=f"job-{job_id}",
            )
            self._thread.start()
            return f"任务 {job_id} 已启动。"

    def _run_guarded(self, job_id: str, failed_only: bool) -> None:
        try:
            run_job(self.config, self.store, job_id, failed_only=failed_only)
        except Exception:
            logger.exception("Unhandled worker failure job_id=%s", job_id)


def run_job(config: Config, store: JobStore, job_id: str, failed_only: bool = False) -> None:
    job_dir = config.jobs_dir / job_id
    lock_path = job_dir / "job.lock"
    lock_path.write_text(str(threading.get_ident()), encoding="utf-8")
    translator = BatchTranslator(config)
    try:
        job = store.load(job_id)
        allowed_indexes = {chapter.index for chapter in job.chapters if chapter.status == "failed"} if failed_only else None
        _prepare_resume(job, failed_only)
        job.status = "running"
        job.cancel_requested = False
        job.started_at = job.started_at or now_ts()
        job.last_error = None
        store.save(job)
        chapter_offsets = _chapter_offsets(job)
        _append_job_log(job, "job started")

        for chapter in job.chapters:
            job = store.load(job_id)
            chapter = job.chapters[chapter.index]
            if failed_only and allowed_indexes is not None and chapter.index not in allowed_indexes:
                continue
            if _should_skip_chapter(chapter.status):
                continue
            if chapter.status == "done" and Path(chapter.translated_path).exists():
                continue
            if job.cancel_requested:
                _mark_cancelled(job, store)
                return

            try:
                _process_chapter(job, chapter.index, translator, store, chapter_offsets.get(chapter.index, 0))
            except JobCancelled:
                job = store.load(job_id)
                _mark_cancelled(job, store)
                return
            except Exception as exc:
                job = store.load(job_id)
                chapter = job.chapters[chapter.index]
                chapter.attempts += 1
                chapter.last_error = f"{type(exc).__name__}: {exc}"
                if job.chapter_failure_policy == "keep_original_on_failed_chapter":
                    chapter.status = "skipped"
                    chapter.finished_at = now_ts()
                    job.last_error = chapter.last_error
                    store.save(job)
                    _append_job_log(job, f"chapter skipped index={chapter.index} error={type(exc).__name__}")
                    continue
                chapter.status = "failed"
                chapter.finished_at = now_ts()
                job.status = "failed"
                job.last_error = chapter.last_error
                job.finished_at = now_ts()
                store.save(job)
                _append_job_log(job, f"job failed chapter={chapter.index} error={type(exc).__name__}")
                return

        job = store.load(job_id)
        if job.cancel_requested:
            _mark_cancelled(job, store)
            return
        output_path = package_job(job, config.output_dir)
        job = store.load(job_id)
        job.output_path = output_path
        job.status = "finished"
        job.finished_at = now_ts()
        job.last_error = None
        store.save(job)
        _append_job_log(job, f"job finished output={output_path}")
    finally:
        if lock_path.exists():
            lock_path.unlink()


def _prepare_resume(job: JobState, failed_only: bool) -> None:
    for chapter in job.chapters:
        translated_path = Path(chapter.translated_path)
        if chapter.status == "done" and not translated_path.exists():
            chapter.status = "pending"
            chapter.done_text_blocks = 0
            chapter.done_batches = 0
        if chapter.status == "failed":
            chapter.status = "pending"
            chapter.done_text_blocks = 0
            chapter.done_batches = 0
            chapter.failed_batches = 0
            chapter.last_error = None


def _should_skip_chapter(status: str) -> bool:
    if status in {"done", "skipped"}:
        return True
    if status == "failed":
        return True
    return False


def _process_chapter(
    job: JobState,
    chapter_index: int,
    translator: BatchTranslator,
    store: JobStore,
    chapter_global_offset: int,
) -> None:
    job = store.load(job.job_id)
    chapter = job.chapters[chapter_index]
    chapter.status = "running"
    chapter.started_at = chapter.started_at or now_ts()
    chapter.last_error = None
    store.save(job)

    soup = parse_xhtml(Path(chapter.abs_path))
    all_blocks = extract_text_blocks(
        soup,
        chapter.id,
        translate_titles=job.translate_titles,
        translate_footnotes=job.translate_footnotes,
    )
    blocks = _select_blocks_for_range(all_blocks, job.translate_start_block, job.translate_end_block, chapter_global_offset)
    batches = make_batches(
        blocks,
        max_items=job.batch_size,
        max_chars=job.max_batch_chars,
        max_tokens=store.config.llm_max_input_tokens,
    )
    translations: dict[str, str] = {}

    chapter.text_blocks = len(blocks)
    chapter.done_text_blocks = 0
    chapter.batches = len(batches)
    chapter.done_batches = 0
    chapter.failed_batches = 0
    store.save(job)
    _append_job_log(job, f"chapter started index={chapter.index} href={chapter.href} blocks={len(blocks)} batches={len(batches)}")

    if not blocks:
        chapter.status = "skipped"
        chapter.finished_at = now_ts()
        store.save(job)
        _append_job_log(job, f"chapter skipped index={chapter.index} reason=no selected text blocks")
        return

    for batch_index, batch in enumerate(batches):
        job = store.load(job.job_id)
        chapter = job.chapters[chapter_index]
        if job.cancel_requested:
            raise JobCancelled("Job cancelled")
        try:
            batch_translations = translator.translate_batch(
                batch,
                target_language=job.target_language,
                mode=job.mode,
                user_prompt=job.user_prompt,
                max_retries=job.max_batch_retries,
            )
            translations.update(batch_translations)
            chapter.done_batches += 1
            chapter.done_text_blocks += len(batch)
            _write_chapter_batch_state(job, chapter_index, batch_index, "done", None)
            store.save(job)
            _append_job_log(job, f"batch done chapter={chapter.index} batch={batch_index}")
        except Exception as exc:
            chapter.failed_batches += 1
            chapter.last_error = f"{type(exc).__name__}: {exc}"
            _write_chapter_batch_state(job, chapter_index, batch_index, "failed", chapter.last_error)
            store.save(job)
            raise

    apply_translations(soup, blocks, translations, job.mode)
    save_xhtml(soup, Path(chapter.translated_path))
    job = store.load(job.job_id)
    chapter = job.chapters[chapter_index]
    chapter.status = "done"
    chapter.done_text_blocks = chapter.text_blocks
    chapter.done_batches = chapter.batches
    chapter.finished_at = now_ts()
    chapter.last_error = None
    store.save(job)


def _write_chapter_batch_state(job: JobState, chapter_index: int, batch_index: int, status: str, error: str | None) -> None:
    chapter = job.chapters[chapter_index]
    state_path = Path(job.translated_dir) / f"{chapter.id}.state.json"
    if state_path.exists():
        from app.utils import read_json

        payload = read_json(state_path)
    else:
        payload = {"chapter_id": chapter.id, "status": "running", "batches": []}
    batches = payload.setdefault("batches", [])
    while len(batches) <= batch_index:
        batches.append({"batch_index": len(batches), "status": "pending", "attempts": 0, "error": None})
    batches[batch_index]["status"] = status
    batches[batch_index]["attempts"] += 1
    batches[batch_index]["error"] = error
    atomic_write_json(state_path, payload)


def _chapter_offsets(job: JobState) -> dict[int, int]:
    offsets: dict[int, int] = {}
    current = 0
    for chapter in job.chapters:
        offsets[chapter.index] = current
        try:
            soup = parse_xhtml(Path(chapter.abs_path))
            count = len(
                extract_text_blocks(
                    soup,
                    chapter.id,
                    translate_titles=job.translate_titles,
                    translate_footnotes=job.translate_footnotes,
                )
            )
        except Exception:
            count = chapter.text_blocks
        current += count
    return offsets


def _select_blocks_for_range(
    blocks: list,
    start_block: int | None,
    end_block: int | None,
    chapter_global_offset: int,
) -> list:
    if start_block is None and end_block is None:
        return blocks
    start = start_block or 1
    end = end_block or 10**12
    selected = []
    for local_index, block in enumerate(blocks, start=1):
        global_index = chapter_global_offset + local_index
        if start <= global_index <= end:
            selected.append(block)
    return selected


def _mark_cancelled(job: JobState, store: JobStore) -> None:
    job.status = "cancelled"
    job.finished_at = now_ts()
    store.save(job)
    _append_job_log(job, "job cancelled")


def _append_job_log(job: JobState, message: str) -> None:
    log_path = Path(job.source_path).parent / "logs.txt"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_ts()} {message}\n")
