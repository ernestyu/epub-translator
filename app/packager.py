from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from app.extractor import inject_bilingual_style
from app.models import JobState
from app.utils import compact_ts, sanitize_filename


def package_job(job: JobState, output_dir: Path) -> str:
    work_dir = Path(job.work_dir)
    translated_dir = Path(job.translated_dir)
    for chapter in job.chapters:
        translated_path = Path(chapter.translated_path)
        if chapter.status == "done" and translated_path.exists():
            target_path = Path(chapter.abs_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(translated_path, target_path)

    for chapter in job.chapters:
        chapter_path = Path(chapter.abs_path)
        if chapter_path.exists():
            inject_bilingual_style(chapter_path)

    result_path = Path(job.result_path)
    tmp_result = result_path.with_name(f"{result_path.name}.tmp")
    _write_epub(work_dir, tmp_result)
    os.replace(tmp_result, result_path)

    safe_stem = sanitize_filename(Path(job.source_filename).stem)
    output_name = f"{safe_stem}.{job.target_language}.{compact_ts()}.bilingual.epub"
    output_path = output_dir / output_name
    output_tmp = output_path.with_name(f"{output_path.name}.tmp")
    shutil.copy2(result_path, output_tmp)
    os.replace(output_tmp, output_path)
    return str(output_path)


def _write_epub(work_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mimetype_path = work_dir / "mimetype"
    if not mimetype_path.exists():
        raise ValueError("Cannot package EPUB without mimetype")

    with zipfile.ZipFile(output_path, "w") as zf:
        zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for file_path in sorted(work_dir.rglob("*")):
            if not file_path.is_file() or file_path == mimetype_path:
                continue
            rel = file_path.relative_to(work_dir).as_posix()
            zf.write(file_path, rel, compress_type=zipfile.ZIP_DEFLATED)
