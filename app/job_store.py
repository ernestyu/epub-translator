from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from app.config import Config
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import extract_text_blocks, parse_xhtml, title_from_soup
from app.models import ChapterState, JobState
from app.utils import atomic_write_json, compact_ts, ensure_within, now_ts, read_json, sanitize_filename


class JobStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.config.ensure_dirs()

    def create_job(
        self,
        uploaded_path: Path,
        original_filename: str,
        source_language: str,
        target_language: str,
        mode: str,
        batch_size: int,
        max_batch_chars: int,
        max_batch_retries: int,
        chapter_failure_policy: str,
        user_prompt: str | None,
        translate_titles: bool,
        translate_footnotes: bool,
        translate_toc: bool,
        translate_start_block: int | None = None,
        translate_end_block: int | None = None,
    ) -> JobState:
        job_id = f"{compact_ts()}-{uuid.uuid4().hex[:8]}"
        job_dir = self.config.jobs_dir / job_id
        work_dir = job_dir / "work"
        translated_dir = job_dir / "translated"
        translated_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = sanitize_filename(original_filename)
        if not safe_filename.lower().endswith(".epub"):
            safe_filename = f"{safe_filename}.epub"
        source_path = job_dir / "source.epub"
        job_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(uploaded_path, source_path)
        unpack_epub(source_path, work_dir)
        epub_info = read_epub_info(work_dir)

        chapters: list[ChapterState] = []
        for chapter in epub_info.chapters:
            chapter_id = f"chapter_{chapter.index:03d}"
            title = None
            text_blocks = 0
            try:
                soup = parse_xhtml(chapter.abs_path)
                title = title_from_soup(soup)
                text_blocks = len(
                    extract_text_blocks(
                        soup,
                        chapter_id,
                        translate_titles=translate_titles,
                        translate_footnotes=translate_footnotes,
                    )
                )
            except Exception:
                title = None
            chapters.append(
                ChapterState(
                    index=chapter.index,
                    id=chapter_id,
                    href=chapter.href,
                    abs_path=str(chapter.abs_path),
                    translated_path=str(translated_dir / f"{chapter_id}.xhtml"),
                    title=title,
                    text_blocks=text_blocks,
                )
            )

        created_at = now_ts()
        job = JobState(
            job_id=job_id,
            status="queued",
            source_filename=safe_filename,
            source_path=str(source_path),
            work_dir=str(work_dir),
            translated_dir=str(translated_dir),
            result_path=str(job_dir / "result.epub"),
            output_path=None,
            source_language=source_language,
            target_language=target_language,
            model=self.config.llm_model,
            base_url=self.config.llm_base_url,
            mode=mode,
            chapter_failure_policy=chapter_failure_policy,
            batch_size=batch_size,
            max_batch_chars=max_batch_chars,
            max_batch_retries=max_batch_retries,
            user_prompt=user_prompt.strip() if user_prompt and user_prompt.strip() else None,
            translate_titles=translate_titles,
            translate_footnotes=translate_footnotes,
            translate_toc=translate_toc,
            translate_start_block=translate_start_block,
            translate_end_block=translate_end_block,
            created_at=created_at,
            updated_at=created_at,
            total_chapters=len(chapters),
            chapters=chapters,
        )
        self.save(job)
        return job

    def path_for(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "state.json"

    def load(self, job_id: str) -> JobState:
        return JobState.from_dict(read_json(self.path_for(job_id)))

    def save(self, job: JobState) -> None:
        job.updated_at = now_ts()
        job.recalculate_progress()
        atomic_write_json(self.path_for(job.job_id), job.to_dict())

    def list_jobs(self) -> list[JobState]:
        jobs: list[JobState] = []
        for state_path in self.config.jobs_dir.glob("*/state.json"):
            try:
                jobs.append(JobState.from_dict(read_json(state_path)))
            except Exception:
                continue
        jobs.sort(key=lambda job: job.updated_at, reverse=True)
        return jobs

    def request_cancel(self, job_id: str) -> JobState:
        job = self.load(job_id)
        job.cancel_requested = True
        if job.status in {"queued", "paused"}:
            job.status = "cancelled"
            job.finished_at = now_ts()
        self.save(job)
        return job

    def delete_job(self, job_id: str) -> None:
        job_dir = self._job_dir(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir)

    def mark_stale_running_as_paused(self) -> None:
        for job in self.list_jobs():
            lock_path = self._job_dir(job.job_id) / "job.lock"
            if job.status in {"queued", "running"}:
                job.status = "paused"
                job.last_error = "Service restarted before this job completed."
                if lock_path.exists():
                    lock_path.unlink()
                self.save(job)

    def _job_dir(self, job_id: str) -> Path:
        return ensure_within(self.config.jobs_dir / job_id, self.config.jobs_dir)
