from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


JobStatus = Literal["queued", "running", "paused", "finished", "failed", "cancelled"]
ChapterStatus = Literal["pending", "running", "done", "failed", "skipped"]
OutputMode = Literal["append_block", "replace"]
FailurePolicy = Literal["stop_on_failed_chapter", "keep_original_on_failed_chapter"]


@dataclass
class TextBlock:
    block_id: str
    tag: str
    text: str
    element: Any = field(repr=False, compare=False, default=None)


@dataclass
class ChapterState:
    index: int
    id: str
    href: str
    abs_path: str
    translated_path: str
    title: str | None = None
    status: ChapterStatus = "pending"
    text_blocks: int = 0
    done_text_blocks: int = 0
    batches: int = 0
    done_batches: int = 0
    failed_batches: int = 0
    attempts: int = 0
    last_error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChapterState":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobState:
    job_id: str
    status: JobStatus
    source_filename: str
    source_path: str
    work_dir: str
    translated_dir: str
    result_path: str
    output_path: str | None
    source_language: str
    target_language: str
    model: str
    base_url: str
    mode: OutputMode
    chapter_failure_policy: FailurePolicy
    batch_size: int
    max_batch_chars: int
    max_batch_retries: int
    user_prompt: str | None
    translate_titles: bool
    translate_footnotes: bool
    translate_toc: bool
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    total_chapters: int = 0
    done_chapters: int = 0
    failed_chapters: int = 0
    skipped_chapters: int = 0
    total_text_blocks: int = 0
    done_text_blocks: int = 0
    last_error: str | None = None
    cancel_requested: bool = False
    translate_start_block: int | None = None
    translate_end_block: int | None = None
    chapters: list[ChapterState] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        data = dict(data)
        data["chapters"] = [ChapterState.from_dict(item) for item in data.get("chapters", [])]
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["chapters"] = [chapter.to_dict() for chapter in self.chapters]
        return data

    def recalculate_progress(self) -> None:
        self.total_chapters = len(self.chapters)
        self.done_chapters = sum(1 for chapter in self.chapters if chapter.status == "done")
        self.failed_chapters = sum(1 for chapter in self.chapters if chapter.status == "failed")
        self.skipped_chapters = sum(1 for chapter in self.chapters if chapter.status == "skipped")
        self.total_text_blocks = sum(chapter.text_blocks for chapter in self.chapters)
        self.done_text_blocks = sum(chapter.done_text_blocks for chapter in self.chapters)
