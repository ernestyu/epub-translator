import os
import shutil
import tempfile
from pathlib import Path

import gradio as gr
from epub_translator import LLM, translate, SubmitKind


API_KEY = os.getenv("LLM_API_KEY", "dummy")
BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
MODEL = os.getenv("LLM_MODEL", "qwen2.5:14b")
TOKEN_ENCODING = os.getenv("TOKEN_ENCODING", "o200k_base")

OUTPUT_DIR = Path("/data/output")
CACHE_DIR = Path("/data/cache")
LOG_DIR = Path("/data/logs")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def translate_epub(file, target_language, mode, user_prompt, concurrency):
    if file is None:
        raise gr.Error("Please upload an EPUB file.")

    source_path = Path(file.name)

    if source_path.suffix.lower() != ".epub":
        raise gr.Error("Only .epub files are supported.")

    safe_name = source_path.stem.replace(" ", "_")
    target_path = OUTPUT_DIR / f"{safe_name}.{target_language}.bilingual.epub"

    submit_mode = {
        "双语块状追加 APPEND_BLOCK": SubmitKind.APPEND_BLOCK,
        "双语行内追加 APPEND_TEXT": SubmitKind.APPEND_TEXT,
        "只保留译文 REPLACE": SubmitKind.REPLACE,
    }[mode]

    llm = LLM(
        key=API_KEY,
        url=BASE_URL,
        model=MODEL,
        token_encoding=TOKEN_ENCODING,
        cache_path=CACHE_DIR,
        timeout=300,
        retry_times=5,
        retry_interval_seconds=6.0,
        log_dir_path=LOG_DIR,
    )

    translate(
        source_path=str(source_path),
        target_path=str(target_path),
        target_language=target_language,
        submit=submit_mode,
        user_prompt=user_prompt.strip() or None,
        concurrency=int(concurrency),
        llm=llm,
    )

    return str(target_path)


with gr.Blocks(title="EPUB Translator") as demo:
    gr.Markdown("# EPUB Translator 本地双语 EPUB 翻译")

    epub_file = gr.File(label="上传 EPUB", file_types=[".epub"])

    target_language = gr.Textbox(
        label="目标语言",
        value="Simplified Chinese",
        placeholder="例如：Simplified Chinese, English, German, Japanese",
    )

    mode = gr.Radio(
        label="输出模式",
        choices=[
            "双语块状追加 APPEND_BLOCK",
            "双语行内追加 APPEND_TEXT",
            "只保留译文 REPLACE",
        ],
        value="双语块状追加 APPEND_BLOCK",
    )

    user_prompt = gr.Textbox(
        label="自定义提示词，可选",
        value="",
        lines=4,
        placeholder="例如：人名保持英文；专业术语按计算机科学语境翻译。",
    )

    concurrency = gr.Slider(
        label="并发数",
        minimum=1,
        maximum=8,
        step=1,
        value=1,
    )

    button = gr.Button("开始翻译")
    output_file = gr.File(label="下载翻译后的 EPUB")

    button.click(
        fn=translate_epub,
        inputs=[epub_file, target_language, mode, user_prompt, concurrency],
        outputs=output_file,
    )

demo.queue().launch(server_name="0.0.0.0", server_port=7860)
