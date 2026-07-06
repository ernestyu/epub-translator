from __future__ import annotations

import logging
import time

from openai import OpenAI

from app.config import Config


logger = logging.getLogger("epub-translator-web.llm")


class LLMClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = OpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            timeout=config.llm_timeout_seconds,
        )

    def chat(self, messages: list[dict[str, str]]) -> str:
        waits = [3, 8, 20]
        last_error: Exception | None = None
        for attempt in range(1, len(waits) + 2):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.llm_model,
                    messages=messages,
                    temperature=self.config.llm_temperature,
                    top_p=self.config.llm_top_p,
                )
                logger.info("LLM request completed attempt=%s", attempt)
                return response.choices[0].message.content or ""
            except Exception as exc:
                last_error = exc
                logger.warning("LLM request failed attempt=%s error=%s", attempt, type(exc).__name__)
                if attempt > len(waits):
                    break
                time.sleep(waits[attempt - 1])
        raise RuntimeError(f"LLM request failed: {last_error}")
