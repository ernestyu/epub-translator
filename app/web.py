from __future__ import annotations

import logging
import sys
from pathlib import Path

import gradio as gr
from openai import OpenAI

from app.config import CONFIG
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import extract_text_blocks, parse_xhtml
from app.job_store import JobStore
from app.preview import build_translated_preview_epub, chapter_summaries, epub_reader_html
from app.settings import (
    FAILURE_POLICY_LABELS,
    OUTPUT_MODE_LABELS,
    PROVIDER_BASE_URLS,
    output_mode_label,
    failure_policy_label,
    save_env_settings,
    update_runtime_config,
)
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
PREVIEW_MAX_CHARS = 5000


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
    user_prompt: str,
    translation_scope_label: str,
    preview_chapter_label,
    preview_start_char,
    preview_char_count,
):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    source_path, original_filename = _uploaded_file_info(epub_file)
    if source_path.suffix.lower() != ".epub":
        raise gr.Error("只支持 .epub 文件。")

    translate_start_block, translate_end_block = _scope_bounds(
        translation_scope_label,
        source_path,
        preview_chapter_label,
        preview_start_char,
        preview_char_count,
        CONFIG.default_translate_titles,
        CONFIG.default_translate_footnotes,
    )
    job = store.create_job(
        uploaded_path=source_path,
        original_filename=sanitize_filename(original_filename),
        source_language=source_language,
        target_language=target_language,
        mode=CONFIG.default_output_mode,
        batch_size=CONFIG.derived_batch_size,
        max_batch_chars=CONFIG.derived_max_batch_chars,
        max_batch_retries=CONFIG.default_batch_retries,
        chapter_failure_policy=CONFIG.default_chapter_failure_policy,
        user_prompt=user_prompt,
        translate_titles=CONFIG.default_translate_titles,
        translate_footnotes=CONFIG.default_translate_footnotes,
        translate_toc=False,
        translate_start_block=translate_start_block,
        translate_end_block=translate_end_block,
    )
    message = worker.start(job.job_id)
    return f"已创建任务：{job.job_id}\n{message}", jobs_table(), job.job_id


def jobs_table():
    rows = []
    for job in store.list_job_summaries():
        rows.append(
            [
                job["job_id"],
                job["source_filename"],
                job["target_language"],
                job["status"],
                f"{job['done_chapters']}/{job['total_chapters']}",
                f"{job['done_text_blocks']}/{job['total_text_blocks']}",
                job["created_at"],
                job["updated_at"],
                "删除：选中本行后点下方删除按钮",
            ]
        )
    return rows


def select_job_from_table(table, evt: gr.SelectData):
    row = _selected_row(table, evt)
    if not row:
        return "", "请选择一个任务。", [], None
    job_id = str(row[0])
    summary, chapters, download = job_detail(job_id)
    return job_id, summary, chapters, download


def _selected_row(table, evt: gr.SelectData):
    try:
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if hasattr(table, "iloc"):
            return table.iloc[row_index].tolist()
        if isinstance(table, dict) and "data" in table:
            return table["data"][row_index]
        return table[row_index]
    except Exception:
        return None


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


def preview_epub(epub_file):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    source_path, original_filename = _uploaded_file_info(epub_file)
    chapters = chapter_summaries(
        source_path,
        CONFIG.default_translate_titles,
        CONFIG.default_translate_footnotes,
        CONFIG,
    )
    chapter_rows = [[chapter.index, chapter.title, chapter.href, chapter.text_blocks, chapter.chars] for chapter in chapters]
    choices = [chapter.label for chapter in chapters]
    summary = "\n".join(
        [
            f"文件: {original_filename}",
            f"章节: {len(chapters)}",
            f"可翻译字符: {sum(chapter.chars for chapter in chapters)}",
            "下面是真实 EPUB 渲染预览；翻译预览会刷新同一个窗口。",
        ]
    )
    return summary, chapter_rows, gr.update(choices=choices, value=choices[0] if choices else None), epub_reader_html(source_path, original_filename)


def preview_translation(
    epub_file,
    target_language: str,
    user_prompt: str,
    preview_chapter_label,
    preview_start_char,
    preview_char_count,
):
    if epub_file is None:
        raise gr.Error("请先上传 EPUB 文件。")
    chapter_index = _chapter_index_from_label(preview_chapter_label)
    start_char, char_count = _clean_char_range(preview_start_char, preview_char_count)
    if char_count > PREVIEW_MAX_CHARS:
        raise gr.Error(f"翻译预览最多一次 {PREVIEW_MAX_CHARS} 字，请缩小范围。")
    source_path, _ = _uploaded_file_info(epub_file)
    preview_epub_path = build_translated_preview_epub(
        source_path=source_path,
        chapter_index=chapter_index,
        start_char=start_char,
        char_count=char_count,
        target_language=target_language,
        mode=CONFIG.default_output_mode,
        batch_size=CONFIG.derived_batch_size,
        max_batch_chars=CONFIG.derived_max_batch_chars,
        max_retries=CONFIG.default_batch_retries,
        user_prompt=user_prompt.strip() or None,
        translate_titles=CONFIG.default_translate_titles,
        translate_footnotes=CONFIG.default_translate_footnotes,
        config=CONFIG,
    )
    message = "\n".join(
        [
            f"已生成翻译预览 EPUB: {preview_epub_path.name}",
            f"章节: {chapter_index}",
            f"字符范围: {start_char} - {start_char + char_count}",
        ]
    )
    return message, epub_reader_html(preview_epub_path, f"Translated preview - chapter {chapter_index}")


def _scope_bounds(
    scope_label: str,
    source_path: Path,
    preview_chapter_label,
    preview_start_char,
    preview_char_count,
    translate_titles: bool,
    translate_footnotes: bool,
) -> tuple[int | None, int | None]:
    if not scope_label.startswith("仅翻译"):
        return None, None
    chapter_index = _chapter_index_from_label(preview_chapter_label)
    start_char, char_count = _clean_char_range(preview_start_char, preview_char_count)
    selected_indexes = [
        global_index
        for global_index, row_chapter_index, _href, _tag, _text, _block, chapter_char_start, chapter_char_end in _collect_preview_blocks(
            source_path, translate_titles, translate_footnotes
        )
        if row_chapter_index == chapter_index
        and chapter_char_end > start_char
        and chapter_char_start < start_char + char_count
    ]
    if not selected_indexes:
        raise gr.Error("当前预览范围内没有可翻译文本。")
    return min(selected_indexes), max(selected_indexes)


def _collect_preview_blocks(source_path: Path, translate_titles: bool, translate_footnotes: bool) -> list[list]:
    import tempfile

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
            chapter_char_cursor = 0
            for block in blocks:
                chapter_char_start = chapter_char_cursor
                chapter_char_end = chapter_char_start + len(block.text)
                rows.append(
                    [
                        global_index,
                        chapter.index,
                        chapter.href,
                        block.tag,
                        block.text,
                        block,
                        chapter_char_start,
                        chapter_char_end,
                    ]
                )
                global_index += 1
                chapter_char_cursor = chapter_char_end
        return rows


def _chapter_index_from_label(label) -> int:
    if label in (None, ""):
        raise gr.Error("请选择一个章节。")
    try:
        return int(str(label).split("|", 1)[0].strip())
    except Exception as exc:
        raise gr.Error("无法识别章节选择。") from exc


def _clean_char_range(start_char, char_count) -> tuple[int, int]:
    start = int(start_char or 0)
    count = int(char_count or 1000)
    if start < 0:
        raise gr.Error("起始字符不能小于 0。")
    if count < 1:
        raise gr.Error("翻译字符数必须大于 0。")
    return start, count


def job_detail(job_id: str):
    if not job_id:
        return "请选择一个任务。", [], None
    try:
        job = store.load(job_id.strip())
    except Exception as exc:
        return f"无法读取任务：{exc}", [], None
    scope = (
        "全书"
        if job.translate_start_block is None
        else f"文本块 {job.translate_start_block} - {job.translate_end_block}"
    )
    summary = "\n".join(
        [
            f"job_id: {job.job_id}",
            f"原文件: {job.source_filename}",
            f"输出文件: {job.output_path or job.result_path}",
            f"源语言 -> 目标语言: {job.source_language} -> {job.target_language}",
            f"模型: {job.model}",
            f"LLM API: {job.base_url} / key={mask_secret(CONFIG.llm_api_key)}",
            f"状态: {job.status}",
            f"翻译范围: {scope}",
            f"输出模式: {job.mode}",
            f"章节失败策略: {job.chapter_failure_policy}",
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


def refresh_selected_job(job_id: str):
    summary, chapters, download = job_detail(job_id)
    return jobs_table(), summary, chapters, download


def resume_job(job_id: str):
    if not job_id:
        raise gr.Error("请先在任务表格里选择一个任务。")
    return worker.start(job_id.strip(), failed_only=False)


def rerun_failed(job_id: str):
    if not job_id:
        raise gr.Error("请先在任务表格里选择一个任务。")
    return worker.start(job_id.strip(), failed_only=True)


def cancel_job(job_id: str):
    if not job_id:
        raise gr.Error("请先在任务表格里选择一个任务。")
    job = store.request_cancel(job_id.strip())
    return f"已请求取消：{job.job_id}，当前状态 {job.status}"


def delete_selected_job(job_id: str):
    if not job_id:
        raise gr.Error("请先在任务表格里选择一个任务。")
    store.delete_job(job_id.strip())
    return f"已删除任务及输出文件：{job_id.strip()}", jobs_table(), "", "请选择一个任务。", [], None


def provider_changed(provider: str):
    base_url = PROVIDER_BASE_URLS.get(provider, "")
    return gr.update(value=base_url) if base_url else gr.update()


def refresh_models(base_url: str, api_key: str, current_model: str):
    try:
        client = OpenAI(api_key=api_key or "ollama", base_url=base_url, timeout=20)
        models = sorted(model.id for model in client.models.list().data)
    except Exception as exc:
        return gr.update(), f"刷新模型失败：{type(exc).__name__}: {exc}"
    value = current_model if current_model in models else (models[0] if models else current_model)
    return gr.update(choices=models, value=value), f"已刷新模型列表，共 {len(models)} 个。"


def test_model(base_url: str, api_key: str, model: str, context_window):
    if not model:
        raise gr.Error("请先选择或填写模型名称。")
    try:
        client = OpenAI(api_key=api_key or "ollama", base_url=base_url, timeout=30)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            temperature=0,
            max_tokens=8,
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        return f"模型测试失败：{type(exc).__name__}: {exc}"
    return f"模型测试成功。返回：{content or '(empty)'}；上下文大小设置为 {int(context_window or CONFIG.llm_context_window)}。"


def save_settings(
    base_url: str,
    api_key: str,
    model: str,
    context_window,
    output_mode_setting: str,
    failure_policy_setting: str,
    translate_titles_setting: bool,
    translate_footnotes_setting: bool,
):
    update_runtime_config(
        CONFIG,
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=int(context_window or 8192),
        output_mode_label_value=output_mode_setting,
        failure_policy_label_value=failure_policy_setting,
        translate_titles=translate_titles_setting,
        translate_footnotes=translate_footnotes_setting,
    )
    env_path = save_env_settings(CONFIG)
    return (
        f"设置已保存到 {env_path.resolve()}，当前进程已立即应用。"
        f"\n自动批次：最多 {CONFIG.derived_batch_size} 个文本块/批，约 {CONFIG.llm_max_input_tokens} input tokens。"
    )


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="EPUB Translator") as demo:
        gr.Markdown("# EPUB Translator 本地双语 EPUB 翻译")

        with gr.Tabs():
            with gr.Tab("新建翻译任务"):
                epub_file = gr.File(label="上传 EPUB", file_types=[".epub"])
                with gr.Row():
                    source_language = gr.Dropdown(LANGUAGES, label="源语言", value=CONFIG.default_source_language)
                    target_language = gr.Dropdown(LANGUAGES, label="目标语言", value=CONFIG.default_target_language)
                user_prompt = gr.Textbox(label="自定义翻译提示词，可选", lines=4)
                with gr.Accordion("EPUB 预览和小范围翻译预览", open=True):
                    preview_button = gr.Button("加载 / 刷新 EPUB 预览")
                    epub_preview_summary = gr.Textbox(label="EPUB 预览摘要", lines=3)
                    epub_reader = gr.HTML(label="EPUB 渲染预览")
                    chapter_table_preview = gr.Dataframe(
                        headers=["章节 index", "标题", "章节 href", "文本块", "可翻译字符"],
                        interactive=False,
                    )
                    preview_chapter = gr.Dropdown(label="预览翻译章节")
                    with gr.Row():
                        preview_start_char = gr.Number(label="章节内起始字符", value=0, precision=0)
                        preview_char_count = gr.Number(label="翻译字符数", value=1000, precision=0)
                    preview_translate_button = gr.Button("翻译预览并刷新阅读器")
                    translation_preview_message = gr.Textbox(label="翻译预览结果", lines=3)
                translation_scope = gr.Radio(
                    ["全书翻译", "仅翻译当前预览章节字数范围"],
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
                        "操作",
                    ],
                    value=jobs_table,
                    interactive=False,
                )
                selected_job_id = gr.Textbox(label="选中的 job id")
                with gr.Row():
                    refresh_selected_button = gr.Button("刷新选中任务")
                    resume_button = gr.Button("继续任务")
                    rerun_button = gr.Button("只重跑失败章节")
                    cancel_button = gr.Button("取消任务")
                    delete_button = gr.Button("删除选中任务及输出文件", variant="stop")
                action_message = gr.Textbox(label="操作结果")
                detail_summary = gr.Textbox(label="任务详情", lines=12)
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

            with gr.Tab("设置"):
                with gr.Accordion("翻译默认设置", open=True):
                    output_mode_setting = gr.Radio(
                        list(OUTPUT_MODE_LABELS.values()),
                        label="输出模式",
                        value=output_mode_label(CONFIG.default_output_mode),
                    )
                    failure_policy_setting = gr.Radio(
                        list(FAILURE_POLICY_LABELS.values()),
                        label="章节失败策略",
                        value=failure_policy_label(CONFIG.default_chapter_failure_policy),
                    )
                    with gr.Row():
                        translate_titles_setting = gr.Checkbox(label="翻译标题", value=CONFIG.default_translate_titles)
                        translate_footnotes_setting = gr.Checkbox(label="翻译脚注", value=CONFIG.default_translate_footnotes)

                with gr.Accordion("LLM 设置", open=True):
                    provider = gr.Dropdown(list(PROVIDER_BASE_URLS.keys()), label="Provider", value="Custom")
                    llm_base_url = gr.Textbox(label="BASE URL", value=CONFIG.llm_base_url)
                    llm_api_key = gr.Textbox(label="API KEY", value=CONFIG.llm_api_key, type="password")
                    with gr.Row():
                        llm_model = gr.Dropdown(
                            choices=[CONFIG.llm_model],
                            value=CONFIG.llm_model,
                            label="模型名称",
                            allow_custom_value=True,
                        )
                        refresh_models_button = gr.Button("刷新模型")
                        test_model_button = gr.Button("测试模型")
                    llm_context_window = gr.Number(label="上下文大小", value=CONFIG.llm_context_window, precision=0)
                    llm_message = gr.Textbox(label="LLM 操作结果", lines=3)

                save_settings_button = gr.Button("保存设置", variant="primary")
                settings_message = gr.Textbox(label="保存结果", lines=3)

        start_button.click(
            create_and_start_job,
            inputs=[
                epub_file,
                source_language,
                target_language,
                user_prompt,
                translation_scope,
                preview_chapter,
                preview_start_char,
                preview_char_count,
            ],
            outputs=[create_message, jobs, created_job_id],
        )
        preview_button.click(
            preview_epub,
            inputs=[epub_file],
            outputs=[epub_preview_summary, chapter_table_preview, preview_chapter, epub_reader],
        )
        epub_file.change(
            preview_epub,
            inputs=[epub_file],
            outputs=[epub_preview_summary, chapter_table_preview, preview_chapter, epub_reader],
        )
        preview_translate_button.click(
            preview_translation,
            inputs=[
                epub_file,
                target_language,
                user_prompt,
                preview_chapter,
                preview_start_char,
                preview_char_count,
            ],
            outputs=[translation_preview_message, epub_reader],
        )
        refresh_jobs.click(jobs_table, outputs=jobs)
        jobs.select(select_job_from_table, inputs=jobs, outputs=[selected_job_id, detail_summary, chapter_table, detail_download])
        refresh_selected_button.click(
            refresh_selected_job,
            inputs=selected_job_id,
            outputs=[jobs, detail_summary, chapter_table, detail_download],
        )
        resume_button.click(resume_job, inputs=selected_job_id, outputs=action_message)
        rerun_button.click(rerun_failed, inputs=selected_job_id, outputs=action_message)
        cancel_button.click(cancel_job, inputs=selected_job_id, outputs=action_message)
        delete_button.click(
            delete_selected_job,
            inputs=selected_job_id,
            outputs=[action_message, jobs, selected_job_id, detail_summary, chapter_table, detail_download],
        )

        provider.change(provider_changed, inputs=provider, outputs=llm_base_url)
        refresh_models_button.click(refresh_models, inputs=[llm_base_url, llm_api_key, llm_model], outputs=[llm_model, llm_message])
        test_model_button.click(test_model, inputs=[llm_base_url, llm_api_key, llm_model, llm_context_window], outputs=llm_message)
        save_settings_button.click(
            save_settings,
            inputs=[
                llm_base_url,
                llm_api_key,
                llm_model,
                llm_context_window,
                output_mode_setting,
                failure_policy_setting,
                translate_titles_setting,
                translate_footnotes_setting,
            ],
            outputs=settings_message,
        )

    return demo


def main() -> None:
    setup_logging()
    store.mark_stale_running_as_paused()
    demo = build_ui()
    demo.queue().launch(
        server_name=CONFIG.app_host,
        server_port=CONFIG.app_port,
        allowed_paths=[str(CONFIG.output_dir.resolve())],
    )
