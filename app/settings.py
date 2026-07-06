from __future__ import annotations

import os
from pathlib import Path

from app.config import CONFIG, Config
from app.i18n import normalize_language, translate
from app.utils import atomic_write_text


OUTPUT_MODE_KEYS = {
    "append_block": "output_append_block",
    "replace": "output_replace",
}

FAILURE_POLICY_KEYS = {
    "stop_on_failed_chapter": "policy_stop",
    "keep_original_on_failed_chapter": "policy_keep_original",
}

PROVIDER_BASE_URLS = {
    "OpenAI": "https://api.openai.com/v1",
    "Ollama": "http://host.docker.internal:11434/v1",
    "LM Studio": "http://host.docker.internal:1234/v1",
    "DeepSeek": "https://api.deepseek.com/v1",
    "OpenRouter": "https://openrouter.ai/api/v1",
    "SiliconFlow": "https://api.siliconflow.cn/v1",
    "Moonshot": "https://api.moonshot.cn/v1",
    "Custom": "",
}


def output_mode_labels(language: str) -> list[str]:
    return [translate(language, key) for key in OUTPUT_MODE_KEYS.values()]


def output_mode_label(value: str, language: str = "zh") -> str:
    return translate(language, OUTPUT_MODE_KEYS.get(value, OUTPUT_MODE_KEYS["append_block"]))


def output_mode_value(label: str) -> str:
    for language in ("zh", "en"):
        for value, key in OUTPUT_MODE_KEYS.items():
            if label == translate(language, key):
                return value
    return "append_block"


def failure_policy_labels(language: str) -> list[str]:
    return [translate(language, key) for key in FAILURE_POLICY_KEYS.values()]


def failure_policy_label(value: str, language: str = "zh") -> str:
    return translate(language, FAILURE_POLICY_KEYS.get(value, FAILURE_POLICY_KEYS["keep_original_on_failed_chapter"]))


def failure_policy_value(label: str) -> str:
    for language in ("zh", "en"):
        for value, key in FAILURE_POLICY_KEYS.items():
            if label == translate(language, key):
                return value
    return "keep_original_on_failed_chapter"


def update_runtime_config(
    config: Config,
    base_url: str,
    api_key: str,
    model: str,
    context_window: int,
    output_mode_label_value: str,
    failure_policy_label_value: str,
    translate_titles: bool,
    translate_footnotes: bool,
    ui_language: str,
) -> None:
    config.llm_base_url = base_url.strip()
    config.llm_api_key = api_key.strip()
    config.llm_model = model.strip()
    config.llm_context_window = int(context_window)
    config.default_output_mode = output_mode_value(output_mode_label_value)
    config.default_chapter_failure_policy = failure_policy_value(failure_policy_label_value)
    config.default_translate_titles = bool(translate_titles)
    config.default_translate_footnotes = bool(translate_footnotes)
    config.ui_language = normalize_language(ui_language)


def save_env_settings(config: Config) -> Path:
    env_path = Path(".env")
    existing = _read_env(env_path)
    updates = {
        "LLM_API_KEY": config.llm_api_key,
        "LLM_BASE_URL": config.llm_base_url,
        "LLM_MODEL": config.llm_model,
        "LLM_CONTEXT_WINDOW": str(config.llm_context_window),
        "TARGET_LANGUAGE": config.default_target_language,
        "SOURCE_LANGUAGE": config.default_source_language,
        "DEFAULT_OUTPUT_MODE": config.default_output_mode,
        "DEFAULT_CHAPTER_FAILURE_POLICY": config.default_chapter_failure_policy,
        "DEFAULT_TRANSLATE_TITLES": _bool_text(config.default_translate_titles),
        "DEFAULT_TRANSLATE_FOOTNOTES": _bool_text(config.default_translate_footnotes),
        "UI_LANGUAGE": config.ui_language,
        "LLM_TIMEOUT_SECONDS": str(config.llm_timeout_seconds),
        "LLM_TEMPERATURE": str(config.llm_temperature),
        "LLM_TOP_P": str(config.llm_top_p),
    }
    existing.update(updates)
    content = "\n".join(f"{key}={value}" for key, value in existing.items()) + "\n"
    atomic_write_text(env_path, content, encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    return env_path


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _bool_text(value: bool) -> str:
    return "true" if value else "false"
