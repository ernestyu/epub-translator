from __future__ import annotations

import logging
import sys
from pathlib import Path

import gradio as gr
from openai import OpenAI

from app.config import CONFIG
from app.epub_io import read_epub_info, unpack_epub
from app.extractor import extract_text_blocks, parse_xhtml
from app.i18n import SUPPORTED_UI_LANGUAGES, TRANSLATIONS, translate
from app.job_store import JobStore
from app.preview import build_translated_preview_epub, chapter_summaries, epub_reader_html
from app.settings import (
    PROVIDER_BASE_URLS,
    failure_policy_labels,
    output_mode_labels,
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
APP_CSS = """
#epub-upload button .wrap {
    font-size: 0 !important;
}

#epub-upload button .wrap::after {
    color: var(--body-text-color);
    content: "EPUB";
    font-size: var(--text-md);
}

#jobs-page,
#settings-page {
    display: none;
}
"""
APP_HEAD = """
<script>
(() => {
    const pageMap = {
        "New Translation": "new-page",
        "Jobs": "jobs-page",
        "Settings": "settings-page",
        "\\u65b0\\u5efa\\u7ffb\\u8bd1\\u4efb\\u52a1": "new-page",
        "\\u4efb\\u52a1\\u5217\\u8868": "jobs-page",
        "\\u8bbe\\u7f6e": "settings-page",
    };
    const panelIds = ["new-page", "jobs-page", "settings-page"];
    const applyPageVisibility = () => {
        const checked = document.querySelector("#page-selector input[type='radio']:checked");
        const activeId = pageMap[checked?.value] || "new-page";
        for (const id of panelIds) {
            const panel = document.getElementById(id);
            if (panel) {
                panel.style.display = id === activeId ? "block" : "none";
            }
        }
    };
    const patchUploadPrompt = () => {
        const prompt = document.querySelector("#epub-upload button .wrap");
        if (prompt && prompt.textContent.trim() !== "EPUB") {
            prompt.textContent = "EPUB";
        }
    };
    const patchUi = () => {
        applyPageVisibility();
        patchUploadPrompt();
    };
    document.addEventListener("DOMContentLoaded", patchUi);
    document.addEventListener("change", patchUi, true);
    document.addEventListener("click", () => setTimeout(patchUi, 0), true);
    new MutationObserver(patchUi).observe(document.documentElement, {
        childList: true,
        subtree: true,
    });
})();
</script>
"""


def t(key: str, **kwargs) -> str:
    return translate(CONFIG.ui_language, key, **kwargs)


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
        raise gr.Error(t("no_epub"))
    source_path, original_filename = _uploaded_file_info(epub_file)
    if source_path.suffix.lower() != ".epub":
        raise gr.Error(t("epub_only"))

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
    message = worker.start(job.job_id, ui_language=CONFIG.ui_language)
    return t("created_job", job_id=job.job_id, message=message), jobs_table(), job.job_id


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
                t("delete_hint"),
            ]
        )
    return rows


def select_job_from_table(table, evt: gr.SelectData):
    row = _selected_row(table, evt)
    if not row:
        return "", t("select_job"), [], None
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
            raise gr.Error(t("cannot_read_upload"))
        path = Path(raw_path)
        return path, file_value.get("orig_name") or path.name
    if hasattr(file_value, "name"):
        path = Path(file_value.name)
        return path, getattr(file_value, "orig_name", path.name)
    raise gr.Error(t("unknown_upload"))


def preview_epub(epub_file):
    if epub_file is None:
        raise gr.Error(t("no_epub"))
    source_path, original_filename = _uploaded_file_info(epub_file)
    chapters = chapter_summaries(
        source_path,
        CONFIG.default_translate_titles,
        CONFIG.default_translate_footnotes,
        CONFIG,
    )
    chapter_rows = [[chapter.index, chapter.title, chapter.href, chapter.text_blocks, chapter.chars] for chapter in chapters]
    choices = [chapter.label for chapter in chapters]
    summary = t(
        "preview_summary_text",
        filename=original_filename,
        chapters=len(chapters),
        chars=sum(chapter.chars for chapter in chapters),
    )
    return (
        summary,
        chapter_rows,
        gr.update(choices=choices, value=choices[0] if choices else None),
        epub_reader_html(source_path, original_filename, CONFIG.ui_language),
    )


def preview_translation(
    epub_file,
    target_language: str,
    user_prompt: str,
    preview_chapter_label,
    preview_start_char,
    preview_char_count,
):
    if epub_file is None:
        raise gr.Error(t("no_epub"))
    chapter_index = _chapter_index_from_label(preview_chapter_label)
    start_char, char_count = _clean_char_range(preview_start_char, preview_char_count)
    if char_count > PREVIEW_MAX_CHARS:
        raise gr.Error(t("preview_chars_limit", limit=PREVIEW_MAX_CHARS))
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
    message = t(
        "preview_done",
        name=preview_epub_path.name,
        chapter=chapter_index,
        start=start_char,
        end=start_char + char_count,
    )
    return message, epub_reader_html(preview_epub_path, f"Translated preview - chapter {chapter_index}", CONFIG.ui_language)


def _scope_bounds(
    scope_label: str,
    source_path: Path,
    preview_chapter_label,
    preview_start_char,
    preview_char_count,
    translate_titles: bool,
    translate_footnotes: bool,
) -> tuple[int | None, int | None]:
    if not _is_preview_scope(scope_label):
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
        raise gr.Error(t("no_text_in_range"))
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


def _is_preview_scope(scope_label: str) -> bool:
    preview_labels = {texts["scope_preview"] for texts in TRANSLATIONS.values()}
    return scope_label in preview_labels


def _chapter_index_from_label(label) -> int:
    if label in (None, ""):
        raise gr.Error(t("select_chapter"))
    try:
        return int(str(label).split("|", 1)[0].strip())
    except Exception as exc:
        raise gr.Error(t("unknown_chapter")) from exc


def _clean_char_range(start_char, char_count) -> tuple[int, int]:
    start = int(start_char or 0)
    count = int(char_count or 1000)
    if start < 0:
        raise gr.Error(t("start_char_invalid"))
    if count < 1:
        raise gr.Error(t("char_count_invalid"))
    return start, count


def job_detail(job_id: str):
    if not job_id:
        return t("select_job"), [], None
    try:
        job = store.load(job_id.strip())
    except Exception as exc:
        return t("load_job_failed", error=exc), [], None
    scope = (
        t("full_book")
        if job.translate_start_block is None
        else t("block_range", start=job.translate_start_block, end=job.translate_end_block)
    )
    summary = "\n".join(
        [
            f"job_id: {job.job_id}",
            f"{t('source_file')}: {job.source_filename}",
            f"{t('download_result')}: {job.output_path or job.result_path}",
            f"{t('source_language')} -> {t('target_language')}: {job.source_language} -> {job.target_language}",
            f"{t('model')}: {job.model}",
            f"LLM API: {job.base_url} / key={mask_secret(CONFIG.llm_api_key)}",
            f"{t('status')}: {job.status}",
            f"{t('translation_scope')}: {scope}",
            f"{t('output_mode')}: {job.mode}",
            f"{t('failure_policy')}: {job.chapter_failure_policy}",
            f"{t('chapter_progress')}: {job.done_chapters}/{job.total_chapters}; {t('text_progress')}: {job.done_text_blocks}/{job.total_text_blocks}",
            f"{t('last_error')}: {job.last_error or ''}",
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
        raise gr.Error(t("select_job_first"))
    return worker.start(job_id.strip(), failed_only=False, ui_language=CONFIG.ui_language)


def rerun_failed(job_id: str):
    if not job_id:
        raise gr.Error(t("select_job_first"))
    return worker.start(job_id.strip(), failed_only=True, ui_language=CONFIG.ui_language)


def cancel_job(job_id: str):
    if not job_id:
        raise gr.Error(t("select_job_first"))
    job = store.request_cancel(job_id.strip())
    return t("cancel_requested", job_id=job.job_id, status=job.status)


def delete_selected_job(job_id: str):
    if not job_id:
        raise gr.Error(t("select_job_first"))
    store.delete_job(job_id.strip())
    return t("deleted_job", job_id=job_id.strip()), jobs_table(), "", t("select_job"), [], None


def provider_changed(provider: str):
    base_url = PROVIDER_BASE_URLS.get(provider, "")
    return gr.update(value=base_url) if base_url else gr.update()


def refresh_models(base_url: str, api_key: str, current_model: str):
    try:
        client = OpenAI(api_key=api_key or "ollama", base_url=base_url, timeout=20)
        models = sorted(model.id for model in client.models.list().data)
    except Exception as exc:
        return gr.update(), t("models_failed", error=f"{type(exc).__name__}: {exc}")
    value = current_model if current_model in models else (models[0] if models else current_model)
    return gr.update(choices=models, value=value), t("models_refreshed", count=len(models))


def test_model(base_url: str, api_key: str, model: str, context_window):
    if not model:
        raise gr.Error(t("model_required"))
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
        return t("model_test_failed", error=f"{type(exc).__name__}: {exc}")
    return t("model_test_ok", content=content or "(empty)", context_window=int(context_window or CONFIG.llm_context_window))


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
        ui_language=CONFIG.ui_language,
    )
    env_path = save_env_settings(CONFIG)
    return t(
        "settings_saved",
        path=env_path.resolve(),
        batch_size=CONFIG.derived_batch_size,
        input_tokens=CONFIG.llm_max_input_tokens,
    )


def page_choices() -> list[str]:
    return [t("tab_new"), t("tab_jobs"), t("tab_settings")]


def switch_ui_language(ui_language: str):
    CONFIG.ui_language = ui_language
    env_path = save_env_settings(CONFIG)
    navigation_choices = page_choices()
    scope_choices = [t("scope_all"), t("scope_preview")]
    job_headers = [
        t("job_id"),
        t("source_file"),
        t("target_language"),
        t("status"),
        t("chapter_progress"),
        t("text_progress"),
        t("created_at"),
        t("updated_at"),
        t("action"),
    ]
    chapter_headers = [
        "index",
        "href",
        t("title"),
        t("status"),
        t("text_blocks"),
        "batch",
        t("failed_batches"),
        t("attempts"),
        t("last_error"),
    ]
    preview_headers = [t("chapter_index"), t("title"), t("chapter_href"), t("text_blocks"), t("translatable_chars")]
    output_choices = output_mode_labels(CONFIG.ui_language)
    failure_choices = failure_policy_labels(CONFIG.ui_language)
    return (
        gr.update(value=f"# {t('app_title')}"),
        gr.update(label=t("navigation"), choices=navigation_choices, value=navigation_choices[0]),
        gr.update(label=t("ui_language")),
        gr.update(label=t("action_result"), value=t("language_switched", path=env_path.resolve())),
        gr.update(label=t("upload_epub")),
        gr.update(label=t("source_language")),
        gr.update(label=t("target_language")),
        gr.update(label=t("custom_prompt")),
        gr.update(value=f"### {t('preview_group')}"),
        gr.update(value=t("load_preview")),
        gr.update(label=t("preview_summary")),
        gr.update(label=t("rendered_preview")),
        gr.update(headers=preview_headers),
        gr.update(label=t("preview_chapter")),
        gr.update(label=t("chapter_start_char")),
        gr.update(label=t("preview_char_count")),
        gr.update(value=t("translate_preview")),
        gr.update(label=t("preview_result")),
        gr.update(label=t("translation_scope"), choices=scope_choices, value=scope_choices[0]),
        gr.update(value=t("start_translation")),
        gr.update(label=t("create_result")),
        gr.update(label=t("new_job_id")),
        gr.update(value=t("refresh_jobs")),
        gr.update(headers=job_headers, value=jobs_table()),
        gr.update(label=t("selected_job")),
        gr.update(value=t("refresh_selected")),
        gr.update(value=t("resume_job")),
        gr.update(value=t("rerun_failed")),
        gr.update(value=t("cancel_job")),
        gr.update(value=t("delete_job")),
        gr.update(label=t("action_result")),
        gr.update(label=t("job_detail")),
        gr.update(headers=chapter_headers),
        gr.update(label=t("download_result")),
        gr.update(value=f"### {t('translation_defaults')}"),
        gr.update(label=t("output_mode"), choices=output_choices, value=output_mode_label(CONFIG.default_output_mode, CONFIG.ui_language)),
        gr.update(label=t("failure_policy"), choices=failure_choices, value=failure_policy_label(CONFIG.default_chapter_failure_policy, CONFIG.ui_language)),
        gr.update(label=t("translate_titles")),
        gr.update(label=t("translate_footnotes")),
        gr.update(value=f"### {t('llm_settings')}"),
        gr.update(label=t("provider")),
        gr.update(label=t("base_url")),
        gr.update(label=t("api_key")),
        gr.update(label=t("model")),
        gr.update(value=t("refresh_models")),
        gr.update(value=t("test_model")),
        gr.update(label=t("context_window")),
        gr.update(label=t("llm_result")),
        gr.update(value=t("save_settings")),
        gr.update(label=t("save_result")),
    )


def build_ui() -> gr.Blocks:
    with gr.Blocks(title=t("app_title")) as demo:
        page_title = gr.Markdown(f"# {t('app_title')}")

        page_selector = gr.Radio(page_choices(), value=t("tab_new"), label=t("navigation"), elem_id="page-selector")

        with gr.Column(elem_id="new-page") as new_page:
            ui_language_selector = gr.Dropdown(
                choices=list(SUPPORTED_UI_LANGUAGES.keys()),
                value=CONFIG.ui_language,
                label=t("ui_language"),
            )
            language_message = gr.Textbox(label=t("action_result"), lines=2)
            epub_file = gr.File(label=t("upload_epub"), file_types=[".epub"], elem_id="epub-upload")
            with gr.Row():
                source_language = gr.Dropdown(LANGUAGES, label=t("source_language"), value=CONFIG.default_source_language)
                target_language = gr.Dropdown(LANGUAGES, label=t("target_language"), value=CONFIG.default_target_language)
            user_prompt = gr.Textbox(label=t("custom_prompt"), lines=4)
            preview_heading = gr.Markdown(f"### {t('preview_group')}")
            with gr.Group():
                preview_button = gr.Button(t("load_preview"))
                epub_preview_summary = gr.Textbox(label=t("preview_summary"), lines=3)
                epub_reader = gr.HTML(label=t("rendered_preview"))
                chapter_table_preview = gr.Dataframe(
                    headers=[t("chapter_index"), t("title"), t("chapter_href"), t("text_blocks"), t("translatable_chars")],
                    interactive=False,
                )
                preview_chapter = gr.Dropdown(label=t("preview_chapter"))
                with gr.Row():
                    preview_start_char = gr.Number(label=t("chapter_start_char"), value=0, precision=0)
                    preview_char_count = gr.Number(label=t("preview_char_count"), value=1000, precision=0)
                preview_translate_button = gr.Button(t("translate_preview"))
                translation_preview_message = gr.Textbox(label=t("preview_result"), lines=3)
            translation_scope = gr.Radio(
                [t("scope_all"), t("scope_preview")],
                label=t("translation_scope"),
                value=t("scope_all"),
            )
            start_button = gr.Button(t("start_translation"), variant="primary")
            create_message = gr.Textbox(label=t("create_result"), lines=3)
            created_job_id = gr.Textbox(label=t("new_job_id"))

        with gr.Column(elem_id="jobs-page") as jobs_page:
            refresh_jobs = gr.Button(t("refresh_jobs"))
            jobs = gr.Dataframe(
                headers=[
                    t("job_id"),
                    t("source_file"),
                    t("target_language"),
                    t("status"),
                    t("chapter_progress"),
                    t("text_progress"),
                    t("created_at"),
                    t("updated_at"),
                    t("action"),
                ],
                value=jobs_table,
                interactive=False,
            )
            selected_job_id = gr.Textbox(label=t("selected_job"))
            with gr.Row():
                refresh_selected_button = gr.Button(t("refresh_selected"))
                resume_button = gr.Button(t("resume_job"))
                rerun_button = gr.Button(t("rerun_failed"))
                cancel_button = gr.Button(t("cancel_job"))
                delete_button = gr.Button(t("delete_job"), variant="stop")
            action_message = gr.Textbox(label=t("action_result"))
            detail_summary = gr.Textbox(label=t("job_detail"), lines=12)
            chapter_table = gr.Dataframe(
                headers=[
                    "index",
                    "href",
                    t("title"),
                    t("status"),
                    t("text_blocks"),
                    "batch",
                    t("failed_batches"),
                    t("attempts"),
                    t("last_error"),
                ],
                interactive=False,
            )
            detail_download = gr.File(label=t("download_result"))

        with gr.Column(elem_id="settings-page") as settings_page:
            translation_defaults_heading = gr.Markdown(f"### {t('translation_defaults')}")
            with gr.Group():
                output_mode_setting = gr.Radio(
                    output_mode_labels(CONFIG.ui_language),
                    label=t("output_mode"),
                    value=output_mode_label(CONFIG.default_output_mode, CONFIG.ui_language),
                )
                failure_policy_setting = gr.Radio(
                    failure_policy_labels(CONFIG.ui_language),
                    label=t("failure_policy"),
                    value=failure_policy_label(CONFIG.default_chapter_failure_policy, CONFIG.ui_language),
                )
                with gr.Row():
                    translate_titles_setting = gr.Checkbox(label=t("translate_titles"), value=CONFIG.default_translate_titles)
                    translate_footnotes_setting = gr.Checkbox(label=t("translate_footnotes"), value=CONFIG.default_translate_footnotes)

            llm_settings_heading = gr.Markdown(f"### {t('llm_settings')}")
            with gr.Group():
                provider = gr.Dropdown(list(PROVIDER_BASE_URLS.keys()), label=t("provider"), value="Custom")
                llm_base_url = gr.Textbox(label=t("base_url"), value=CONFIG.llm_base_url)
                llm_api_key = gr.Textbox(label=t("api_key"), value=CONFIG.llm_api_key, type="password")
                with gr.Row():
                    llm_model = gr.Dropdown(
                        choices=[CONFIG.llm_model],
                        value=CONFIG.llm_model,
                        label=t("model"),
                        allow_custom_value=True,
                    )
                    refresh_models_button = gr.Button(t("refresh_models"))
                    test_model_button = gr.Button(t("test_model"))
                llm_context_window = gr.Number(label=t("context_window"), value=CONFIG.llm_context_window, precision=0)
                llm_message = gr.Textbox(label=t("llm_result"), lines=3)

            save_settings_button = gr.Button(t("save_settings"), variant="primary")
            settings_message = gr.Textbox(label=t("save_result"), lines=3)

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
        ui_language_selector.change(
            switch_ui_language,
            inputs=ui_language_selector,
            outputs=[
                page_title,
                page_selector,
                ui_language_selector,
                language_message,
                epub_file,
                source_language,
                target_language,
                user_prompt,
                preview_heading,
                preview_button,
                epub_preview_summary,
                epub_reader,
                chapter_table_preview,
                preview_chapter,
                preview_start_char,
                preview_char_count,
                preview_translate_button,
                translation_preview_message,
                translation_scope,
                start_button,
                create_message,
                created_job_id,
                refresh_jobs,
                jobs,
                selected_job_id,
                refresh_selected_button,
                resume_button,
                rerun_button,
                cancel_button,
                delete_button,
                action_message,
                detail_summary,
                chapter_table,
                detail_download,
                translation_defaults_heading,
                output_mode_setting,
                failure_policy_setting,
                translate_titles_setting,
                translate_footnotes_setting,
                llm_settings_heading,
                provider,
                llm_base_url,
                llm_api_key,
                llm_model,
                refresh_models_button,
                test_model_button,
                llm_context_window,
                llm_message,
                save_settings_button,
                settings_message,
            ],
        )

    return demo


def main() -> None:
    setup_logging()
    store.mark_stale_running_as_paused()
    demo = build_ui()
    demo.queue().launch(
        server_name=CONFIG.app_host,
        server_port=CONFIG.app_port,
        footer_links=[],
        css=APP_CSS,
        head=APP_HEAD,
        allowed_paths=[str(CONFIG.output_dir.resolve())],
    )
