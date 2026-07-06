from __future__ import annotations

import html
import logging
import sys
import tempfile
from pathlib import Path

import gradio as gr

from app.batcher import make_batches
from app.config import CONFIG
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import extract_text_blocks, parse_xhtml
from app.job_store import JobStore
from app.translator import BatchTranslator
from app.utils import mask_secret, sanitize_filename
from app.worker import WorkerManager


LANGUAGES = [
    "Simplified Chinese",
    "Traditional Chinese",
    "English",
    "Japanese",
    "French",
    "German",
    "Spanish",
    "Korean",
    "Italian",
    "Portuguese",
]
PREVIEW_MAX_TRANSLATE_BLOCKS = 40


def setup_logging() -> None:
    CONFIG.ensure_dirs()
    logging.basicConfig(
        level=CONFIG.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(CONFIG.logs_dir / "app.log", encoding="utf-8"),
        ],
    )
    logger = logging.getLogger("epub-translator-web")
    logger.info("Starting EPUB Translator WebUI")
    logger.info("LLM_BASE_URL=%s", CONFIG.llm_base_url)
    logger.info("LLM_MODEL=%s", CONFIG.llm_model)
    logger.info("DATA_DIR=%s", CONFIG.data_dir)


store = JobStore(CONFIG)
worker = WorkerManager(CONFIG, store)


def create_and_start_job(
    epub_file,
    source_language: str,
    target_language: str,
    mode_label: str,
    batch_size: int,
    max_batch_chars: int,
    max_batch_retries: int,
    failure_policy_label: str,
    user_prompt: str,
    translate_titles: bool,
    translate_footnotes: bool,
    translation_scope_label: str,
    start_block,
    end_block,
):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    source_path, original_filename = _uploaded_file_info(epub_file)
    if source_path.suffix.lower() != ".epub":
        raise gr.Error("只支持 .epub 文件。")

    mode = "append_block" if mode_label.startswith("append_block") else "replace"
    failure_policy = (
        "keep_original_on_failed_chapter"
        if failure_policy_label.startswith("keep_original")
        else "stop_on_failed_chapter"
    )
    translate_start_block, translate_end_block = _scope_bounds(translation_scope_label, start_block, end_block)
    job = store.create_job(
        uploaded_path=source_path,
        original_filename=sanitize_filename(original_filename),
        source_language=source_language,
        target_language=target_language,
        mode=mode,
        batch_size=int(batch_size),
        max_batch_chars=int(max_batch_chars),
        max_batch_retries=int(max_batch_retries),
        chapter_failure_policy=failure_policy,
        user_prompt=user_prompt,
        translate_titles=translate_titles,
        translate_footnotes=translate_footnotes,
        translate_toc=False,
        translate_start_block=translate_start_block,
        translate_end_block=translate_end_block,
    )
    message = worker.start(job.job_id)
    return f"已创建任务：{job.job_id}\n{message}", jobs_table(), job.job_id


def jobs_table():
    rows = []
    for job in store.list_jobs():
        rows.append(
            [
                job.job_id,
                job.source_filename,
                job.target_language,
                job.status,
                f"{job.done_chapters}/{job.total_chapters}",
                f"{job.done_text_blocks}/{job.total_text_blocks}",
                job.created_at,
                job.updated_at,
                job.output_path or "",
            ]
        )
    return rows


def _uploaded_file_info(file_value) -> tuple[Path, str]:
    if isinstance(file_value, (str, Path)):
        path = Path(file_value)
        return path, path.name
    if isinstance(file_value, dict):
        raw_path = file_value.get("path") or file_value.get("name")
        if not raw_path:
            raise gr.Error("无法读取上传文件路径。")
        path = Path(raw_path)
        return path, file_value.get("orig_name") or path.name
    if hasattr(file_value, "name"):
        path = Path(file_value.name)
        return path, getattr(file_value, "orig_name", path.name)
    raise gr.Error("无法识别上传文件。")


def preview_epub(epub_file, translate_titles: bool, translate_footnotes: bool):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    source_path, original_filename = _uploaded_file_info(epub_file)
    rows = _collect_preview_rows(source_path, translate_titles, translate_footnotes)
    summary = "\n".join(
        [
            f"文件: {original_filename}",
            f"可翻译文本块: {len(rows)}",
            "EPUB 没有固定页码；这里用全书文本块编号作为预览和范围翻译依据。",
        ]
    )
    return summary, rows[:200]


def preview_translation(
    epub_file,
    target_language: str,
    mode_label: str,
    batch_size,
    max_batch_chars,
    max_batch_retries,
    user_prompt: str,
    translate_titles: bool,
    translate_footnotes: bool,
    start_block,
    end_block,
):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    start, end = _clean_range(start_block, end_block)
    if end - start + 1 > PREVIEW_MAX_TRANSLATE_BLOCKS:
        raise gr.Error(f"预览翻译最多一次 {PREVIEW_MAX_TRANSLATE_BLOCKS} 个文本块，请缩小范围。")

    source_path, _ = _uploaded_file_info(epub_file)
    rows = _collect_preview_rows(source_path, translate_titles, translate_footnotes)
    selected_rows = [row for row in rows if start <= row[0] <= end]
    if not selected_rows:
        raise gr.Error("这个范围内没有可翻译文本块。")

    blocks = [
        row[5]
        for row in _collect_preview_blocks(source_path, translate_titles, translate_footnotes)
        if start <= row[0] <= end
    ]
    translator = BatchTranslator(CONFIG)
    translations: dict[str, str] = {}
    for batch in make_batches(blocks, max_items=int(batch_size), max_chars=int(max_batch_chars)):
        translations.update(
            translator.translate_batch(
                batch,
                target_language=target_language,
                mode="append_block" if mode_label.startswith("append_block") else "replace",
                user_prompt=user_prompt,
                max_retries=int(max_batch_retries),
            )
        )
    preview_rows = [
        [row[0], row[1], row[3], row[4], translations.get(row[5].block_id, "")]
        for row in _collect_preview_blocks(source_path, translate_titles, translate_footnotes)
        if start <= row[0] <= end
    ]
    return _translation_preview_html(preview_rows), preview_rows


def _collect_preview_rows(source_path: Path, translate_titles: bool, translate_footnotes: bool) -> list[list]:
    return [
        [global_index, chapter_index, href, tag, text]
        for global_index, chapter_index, href, tag, text, _block in _collect_preview_blocks(
            source_path, translate_titles, translate_footnotes
        )
    ]


def _collect_preview_blocks(source_path: Path, translate_titles: bool, translate_footnotes: bool) -> list[list]:
    with tempfile.TemporaryDirectory(dir=CONFIG.cache_dir) as tmp:
        work_dir = Path(tmp) / "work"
        unpack_epub(source_path, work_dir)
        epub_info = read_epub_info(work_dir)
        rows = []
        global_index = 1
        for chapter in epub_info.chapters:
            soup = parse_xhtml(chapter.abs_path)
            chapter_id = f"chapter_{chapter.index:03d}"
            blocks = extract_text_blocks(
                soup,
                chapter_id,
                translate_titles=translate_titles,
                translate_footnotes=translate_footnotes,
            )
            for block in blocks:
                rows.append([global_index, chapter.index, chapter.href, block.tag, block.text, block])
                global_index += 1
        return rows


def _translation_preview_html(rows: list[list]) -> str:
    parts = [
        "<table style='width:100%;border-collapse:collapse'>",
        "<thead><tr><th>编号</th><th>章节</th><th>标签</th><th>原文</th><th>译文</th></tr></thead><tbody>",
    ]
    for global_index, chapter_index, tag, source, translation in rows:
        parts.append(
            "<tr>"
            f"<td style='vertical-align:top;border:1px solid #ddd;padding:6px'>{global_index}</td>"
            f"<td style='vertical-align:top;border:1px solid #ddd;padding:6px'>{chapter_index}</td>"
            f"<td style='vertical-align:top;border:1px solid #ddd;padding:6px'>{html.escape(tag)}</td>"
            f"<td style='vertical-align:top;border:1px solid #ddd;padding:6px'>{html.escape(source)}</td>"
            f"<td style='vertical-align:top;border:1px solid #ddd;padding:6px'>{html.escape(translation)}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def _scope_bounds(scope_label: str, start_block, end_block) -> tuple[int | None, int | None]:
    if not scope_label.startswith("仅翻译"):
        return None, None
    return _clean_range(start_block, end_block)


def _clean_range(start_block, end_block) -> tuple[int, int]:
    start = int(start_block or 1)
    end = int(end_block or start)
    if start < 1 or end < 1:
        raise gr.Error("文本块编号必须大于 0。")
    if end < start:
        raise gr.Error("结束编号不能小于开始编号。")
    return start, end


def job_detail(job_id: str):
    if not job_id:
        return "请输入或选择 job id。", [], None
    try:
        job = store.load(job_id.strip())
    except Exception as exc:
        return f"无法读取任务：{exc}", [], None
    summary = "\n".join(
        [
            f"job_id: {job.job_id}",
            f"原文件: {job.source_filename}",
            f"输出文件: {job.output_path or job.result_path}",
            f"源语言 -> 目标语言: {job.source_language} -> {job.target_language}",
            f"模型: {job.model}",
            f"LLM API: {job.base_url} / key={mask_secret(CONFIG.llm_api_key)}",
            f"状态: {job.status}",
            f"进度: 章节 {job.done_chapters}/{job.total_chapters}, 文本块 {job.done_text_blocks}/{job.total_text_blocks}",
            f"最近错误: {job.last_error or ''}",
        ]
    )
    chapters = [
        [
            chapter.index,
            chapter.href,
            chapter.title or "",
            chapter.status,
            chapter.text_blocks,
            f"{chapter.done_batches}/{chapter.batches}",
            chapter.failed_batches,
            chapter.attempts,
            chapter.last_error or "",
        ]
        for chapter in job.chapters
    ]
    download = job.output_path if job.output_path and Path(job.output_path).exists() else None
    return summary, chapters, download


def resume_job(job_id: str):
    if not job_id:
        raise gr.Error("请输入 job id。")
    return worker.start(job_id.strip(), failed_only=False)


def rerun_failed(job_id: str):
    if not job_id:
        raise gr.Error("请输入 job id。")
    return worker.start(job_id.strip(), failed_only=True)


def cancel_job(job_id: str):
    if not job_id:
        raise gr.Error("请输入 job id。")
    job = store.request_cancel(job_id.strip())
    return f"已请求取消：{job.job_id}，当前状态 {job.status}"


def delete_job(job_id: str):
    if not job_id:
        raise gr.Error("请输入 job id。")
    store.delete_job(job_id.strip())
    return f"已删除任务：{job_id.strip()}", jobs_table()


def output_files_table():
    files = sorted(CONFIG.output_dir.glob("*.epub"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [[path.name, f"{path.stat().st_size / 1024 / 1024:.2f} MB", str(path)] for path in files]


def choose_output(table, evt: gr.SelectData):
    try:
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if hasattr(table, "iloc"):
            return table.iloc[row_index, 2]
        if isinstance(table, dict) and "data" in table:
            return table["data"][row_index][2]
        return table[row_index][2]
    except Exception:
        return None


def delete_output(path_value: str):
    if not path_value:
        raise gr.Error("请先选择文件。")
    path = Path(path_value)
    if path.parent.resolve() != CONFIG.output_dir.resolve():
        raise gr.Error("非法输出路径。")
    if path.exists():
        path.unlink()
    return output_files_table(), None


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="EPUB Translator") as demo:
        gr.Markdown("# EPUB Translator 本地双语 EPUB 翻译")

        with gr.Tabs():
            with gr.Tab("新建翻译任务"):
                epub_file = gr.File(label="上传 EPUB", file_types=[".epub"])
                with gr.Row():
                    source_language = gr.Dropdown(LANGUAGES, label="源语言", value=CONFIG.default_source_language)
                    target_language = gr.Dropdown(LANGUAGES, label="目标语言", value=CONFIG.default_target_language)
                mode = gr.Radio(
                    ["append_block：原文段落后追加译文", "replace：只保留译文"],
                    label="输出模式",
                    value="append_block：原文段落后追加译文",
                )
                with gr.Row():
                    batch_size = gr.Number(label="批次大小", value=CONFIG.default_batch_size, precision=0)
                    max_batch_chars = gr.Number(label="每批最大字符数", value=CONFIG.default_max_batch_chars, precision=0)
                    max_batch_retries = gr.Number(label="每批最大重试次数", value=CONFIG.default_batch_retries, precision=0)
                failure_policy = gr.Radio(
                    [
                        "stop_on_failed_chapter：失败则停止任务",
                        "keep_original_on_failed_chapter：失败章节保留原文并继续",
                    ],
                    label="章节失败策略",
                    value=(
                        "keep_original_on_failed_chapter：失败章节保留原文并继续"
                        if CONFIG.default_chapter_failure_policy == "keep_original_on_failed_chapter"
                        else "stop_on_failed_chapter：失败则停止任务"
                    ),
                )
                user_prompt = gr.Textbox(label="自定义翻译提示词，可选", lines=4)
                with gr.Row():
                    translate_titles = gr.Checkbox(label="翻译标题", value=True)
                    translate_footnotes = gr.Checkbox(label="翻译脚注", value=True)
                with gr.Accordion("上传预览和小范围翻译预览", open=True):
                    preview_button = gr.Button("预览 EPUB 内容")
                    epub_preview_summary = gr.Textbox(label="EPUB 预览摘要", lines=3)
                    epub_preview_table = gr.Dataframe(
                        headers=["文本块编号", "章节 index", "章节 href", "标签", "原文"],
                        interactive=False,
                    )
                    with gr.Row():
                        preview_start_block = gr.Number(
                            label="预览起始文本块编号（近似第几页）",
                            value=1,
                            precision=0,
                        )
                        preview_end_block = gr.Number(
                            label="预览结束文本块编号（近似第几页）",
                            value=8,
                            precision=0,
                        )
                    preview_translate_button = gr.Button("预览这段翻译效果")
                    translation_preview_html = gr.HTML(label="翻译效果预览")
                    translation_preview_table = gr.Dataframe(
                        headers=["文本块编号", "章节 index", "标签", "原文", "译文"],
                        interactive=False,
                    )
                translation_scope = gr.Radio(
                    ["全书翻译", "仅翻译上面文本块范围"],
                    label="正式任务翻译范围",
                    value="全书翻译",
                )
                start_button = gr.Button("开始翻译", variant="primary")
                create_message = gr.Textbox(label="创建结果", lines=3)
                created_job_id = gr.Textbox(label="新任务 job id")

            with gr.Tab("任务列表"):
                refresh_jobs = gr.Button("刷新任务列表")
                jobs = gr.Dataframe(
                    headers=[
                        "job id",
                        "原文件名",
                        "目标语言",
                        "状态",
                        "章节进度",
                        "文本块进度",
                        "创建时间",
                        "更新时间",
                        "输出路径",
                    ],
                    value=jobs_table,
                    interactive=False,
                )

            with gr.Tab("任务详情"):
                detail_job_id = gr.Textbox(label="job id")
                with gr.Row():
                    detail_button = gr.Button("查看详情")
                    resume_button = gr.Button("继续任务")
                    rerun_button = gr.Button("只重跑失败章节")
                    cancel_button = gr.Button("取消任务")
                    delete_button = gr.Button("删除任务")
                action_message = gr.Textbox(label="操作结果")
                detail_summary = gr.Textbox(label="任务摘要", lines=10)
                chapter_table = gr.Dataframe(
                    headers=[
                        "index",
                        "href",
                        "标题",
                        "状态",
                        "文本块",
                        "batch",
                        "失败 batch",
                        "尝试次数",
                        "最近错误",
                    ],
                    interactive=False,
                )
                detail_download = gr.File(label="下载结果 EPUB")

            with gr.Tab("已完成文件"):
                refresh_outputs = gr.Button("刷新文件列表")
                outputs = gr.Dataframe(headers=["文件名", "大小", "路径"], value=output_files_table, interactive=False)
                selected_output = gr.Textbox(label="选中的文件路径", visible=False)
                output_download = gr.File(label="下载选中文件")
                delete_output_button = gr.Button("删除选中文件")

            with gr.Tab("LLM 设置"):
                gr.Textbox(label="LLM API 地址", value=CONFIG.llm_base_url, interactive=False)
                gr.Textbox(label="模型名称", value=CONFIG.llm_model, interactive=False)
                gr.Textbox(label="API key", value=mask_secret(CONFIG.llm_api_key), interactive=False)
                gr.Markdown("第一版从环境变量读取 LLM 设置；修改 `.env` 后重启服务生效。")

        start_button.click(
            create_and_start_job,
            inputs=[
                epub_file,
                source_language,
                target_language,
                mode,
                batch_size,
                max_batch_chars,
                max_batch_retries,
                failure_policy,
                user_prompt,
                translate_titles,
                translate_footnotes,
                translation_scope,
                preview_start_block,
                preview_end_block,
            ],
            outputs=[create_message, jobs, created_job_id],
        )
        preview_button.click(
            preview_epub,
            inputs=[epub_file, translate_titles, translate_footnotes],
            outputs=[epub_preview_summary, epub_preview_table],
        )
        preview_translate_button.click(
            preview_translation,
            inputs=[
                epub_file,
                target_language,
                mode,
                batch_size,
                max_batch_chars,
                max_batch_retries,
                user_prompt,
                translate_titles,
                translate_footnotes,
                preview_start_block,
                preview_end_block,
            ],
            outputs=[translation_preview_html, translation_preview_table],
        )
        refresh_jobs.click(jobs_table, outputs=jobs)
        detail_button.click(job_detail, inputs=detail_job_id, outputs=[detail_summary, chapter_table, detail_download])
        resume_button.click(resume_job, inputs=detail_job_id, outputs=action_message)
        rerun_button.click(rerun_failed, inputs=detail_job_id, outputs=action_message)
        cancel_button.click(cancel_job, inputs=detail_job_id, outputs=action_message)
        delete_button.click(delete_job, inputs=detail_job_id, outputs=[action_message, jobs])
        refresh_outputs.click(output_files_table, outputs=outputs)
        outputs.select(choose_output, inputs=outputs, outputs=[selected_output])
        selected_output.change(lambda path: path if path else None, inputs=selected_output, outputs=output_download)
        delete_output_button.click(delete_output, inputs=selected_output, outputs=[outputs, output_download])

    return demo


def main() -> None:
    setup_logging()
    store.mark_stale_running_as_paused()
    demo = build_ui()
    demo.queue().launch(server_name=CONFIG.app_host, server_port=CONFIG.app_port)
