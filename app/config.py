import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass
class Config:
    data_dir: Path
    jobs_dir: Path
    output_dir: Path
    cache_dir: Path
    logs_dir: Path
    app_host: str
    app_port: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_context_window: int
    llm_timeout_seconds: int
    llm_temperature: float
    llm_top_p: float
    default_source_language: str
    default_target_language: str
    default_output_mode: str
    default_batch_size: int
    default_max_batch_chars: int
    default_batch_retries: int
    default_chapter_failure_policy: str
    default_translate_titles: bool
    default_translate_footnotes: bool
    ui_language: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            jobs_dir=data_dir / "jobs",
            output_dir=data_dir / "output",
            cache_dir=data_dir / "cache",
            logs_dir=data_dir / "logs",
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "7860")),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1"),
            llm_api_key=os.getenv("LLM_API_KEY", "ollama"),
            llm_model=os.getenv("LLM_MODEL", "qwen3:32b"),
            llm_context_window=int(os.getenv("LLM_CONTEXT_WINDOW", "8192")),
            llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "300")),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
            llm_top_p=float(os.getenv("LLM_TOP_P", "0.8")),
            default_source_language=os.getenv("SOURCE_LANGUAGE", "English"),
            default_target_language=os.getenv("TARGET_LANGUAGE", "Simplified Chinese"),
            default_output_mode=os.getenv("DEFAULT_OUTPUT_MODE", "append_block"),
            default_batch_size=int(os.getenv("DEFAULT_BATCH_SIZE", "8")),
            default_max_batch_chars=int(os.getenv("DEFAULT_MAX_BATCH_CHARS", "6000")),
            default_batch_retries=int(os.getenv("DEFAULT_BATCH_RETRIES", "3")),
            default_chapter_failure_policy=os.getenv(
                "DEFAULT_CHAPTER_FAILURE_POLICY", "keep_original_on_failed_chapter"
            ),
            default_translate_titles=_env_bool("DEFAULT_TRANSLATE_TITLES", True),
            default_translate_footnotes=_env_bool("DEFAULT_TRANSLATE_FOOTNOTES", True),
            ui_language=os.getenv("UI_LANGUAGE", "zh"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def ensure_dirs(self) -> None:
        for path in (self.jobs_dir, self.output_dir, self.cache_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def llm_reserved_output_tokens(self) -> int:
        return max(1024, min(4096, int(self.llm_context_window * 0.36)))

    @property
    def llm_max_input_tokens(self) -> int:
        prompt_overhead = max(700, int(self.llm_context_window * 0.1))
        safety_margin = max(500, int(self.llm_context_window * 0.1))
        budget = self.llm_context_window - self.llm_reserved_output_tokens - prompt_overhead - safety_margin
        return max(512, budget)

    @property
    def derived_batch_size(self) -> int:
        return max(4, min(24, self.llm_max_input_tokens // 220))

    @property
    def derived_max_batch_chars(self) -> int:
        return max(1500, min(12000, self.llm_max_input_tokens * 3))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


CONFIG = Config.from_env()
