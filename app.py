import logging
import os
import sys
import time
from pathlib import Path

import gradio as gr
from epub_translator import LLM, translate, SubmitKind


# -----------------------------
# Logging
# -----------------------------

LOG_DIR = Path("/data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "app.log"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

logger = logging.getLogger("epub-translator-web")


# -----------------------------
# Environment
# -----------------------------

API_KEY = os.getenv("LLM_API_KEY", "dummy")
BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
MODEL = os.getenv("LLM_MODEL", "qwen2.5:14b")
TOKEN_ENCODING = os.getenv("TOKEN_ENCODING", "o200k_base")

OUTPUT_DIR = Path("/data/output")
CACHE_DIR = Path("/data/cache")
TRANSLATOR_LOG_DIR = Path("/data/logs/translator")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
TRANSLATOR_LOG_DIR.mkdir(parents=True, exist_ok=True)


logger.info("Starting EPUB Translator WebUI")
logger.info("LLM_BASE_URL=%s", BASE_URL)
logger.info("LLM_MODEL=%s", MODEL)
logger.info("TOKEN_ENCODING=%s", TOKEN_ENCODING)
logger.info("APP_LOG_FILE=%s", LOG_FILE)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + value[-3:]


def translate_epub(file, target_language, mode, user_prompt, concurrency, progress=gr.Progress()):
    start_time = time.time()

    logger.info("Received translation request")

    progress(0.02, desc="Checking uploaded file")

    if file is None:
        logger.warning("No EPUB file uploaded")
        raise gr.Error("Please upload an EPUB file.")

    source_path = Path(file.name)
    logger.info("Uploaded file path: %s", source_path)

    if source_path.suffix.lower() != ".epub":
        logger.warning("Invalid file type: %s", source_path.suffix)
        raise gr.Error("Only .epub files are supported.")

    safe_name = source_path.stem.replace(" ", "_")
    target_path = OUTPUT_DIR / f"{safe_name}.{target_language}.bilingual.epub"

    logger.info("Source EPUB: %s", source_path)
    logger.info("Target EPUB: %s", target_path)
    logger.info("Target language: %s", target_language)
    logger.info("Output mode: %s", mode)
    logger.info("Concurrency: %s", concurrency)
    logger.info("LLM API key: %s", mask_secret(API_KEY))
    logger.info("LLM base URL: %s", BASE_URL)
    logger.info("LLM model: %s", MODEL)

    progress(0.08, desc="Preparing output mode")

    submit_mode = {
        "双语块状追加 APPEND_BLOCK": SubmitKind.APPEND_BLOCK,
        "双语行内追加 APPEND_TEXT": SubmitKind.APPEND_TEXT,
        "只保留译文 REPLACE": SubmitKind.REPLACE,
    }[mode]

    progress(0.12, desc="Initializing LLM client")

    llm = LLM(
        key=API_KEY,
        url=BASE_URL,
        model=MODEL,
        token_encoding=TOKEN_ENCODING,
        cache_path=CACHE_DIR,
        timeout=300,
        retry_times=5,
        retry_interval_seconds=6.0,
        log_dir_path=TRANSLATOR_LOG_DIR,
    )

    logger.info("LLM client initialized")
    logger.info("Translator internal log dir: %s", TRANSLATOR_LOG_DIR)

    if user_prompt and user_prompt.strip():
        logger.info("Custom user prompt is enabled")
    else:
        logger.info("No custom user prompt")

    progress(0.18, desc="Starting translation. This may take a while")

    try:
        logger.info("Calling epub_translator.translate()")

        translate(
            source_path=str(source_path),
            target_path=str(target_path),
            target_language=target_language,
            submit=submit_mode,
            user_prompt=user_prompt.strip() or None,
            concurrency=int(concurrency),
            llm=llm,
        )

        elapsed = time.time() - start_time

        if target_path.exists():
            size_mb = target_path.stat().st_size / 1024 / 1024
            logger.info("Translation finished successfully")
            logger.info("Output file: %s", target_path)
            logger.info("Output size: %.2f MB", size_mb)
            logger.info("Elapsed time: %.1f seconds", elapsed)
        else:
            logger.error("Translation function returned, but output file does not exist: %s", target_path)
            raise gr.Error("Translation finished, but output file was not created.")

        progress(1.0, desc="Done")
        return str(target_path)

    except Exception as exc:
        elapsed = time.time() - start_time
        logger.exception("Translation failed after %.1f seconds", elapsed)
        raise gr.Error(f"Translation failed: {type(exc).__name__}: {exc}")


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
