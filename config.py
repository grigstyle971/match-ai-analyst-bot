"""Загрузка настроек из переменных окружения (.env)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class LLMProvider:
    base_url: str | None  # None = официальный API OpenAI
    api_key: str
    model: str
    reasoning_effort: str | None  # None = не передавать параметр (не все API его знают)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    llm_chain: list[LLMProvider]  # основная модель, затем запасные — по порядку
    llm_timeout: float
    max_input_chars: int
    cooldown_seconds: float
    webapp_port: int  # 0 = Mini App выключено
    webapp_url: str | None  # свой HTTPS-адрес; пусто = поднять туннель Cloudflare автоматически

    @property
    def llm_model(self) -> str:
        return self.llm_chain[0].model


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_settings() -> Settings:
    bot_token = _env("BOT_TOKEN")
    llm_api_key = _env("LLM_API_KEY")

    missing = [name for name, value in (("BOT_TOKEN", bot_token), ("LLM_API_KEY", llm_api_key)) if not value]
    if missing:
        raise RuntimeError(
            f"Не заданы переменные окружения: {', '.join(missing)}. "
            "Скопируйте .env.example в .env и заполните их."
        )

    model_name = _env("LLM_MODEL", "gemini-3-flash-preview")
    # Если в окружении задана упершаяся в суточный лимит Free Tier модель (3.6/3.5), переключаем на быструю 3-flash-preview
    if model_name in ("gemini-3.6-flash", "gemini-3.5-flash"):
        model_name = "gemini-3-flash-preview"

    main = LLMProvider(
        base_url=_env("LLM_BASE_URL") or None,
        api_key=llm_api_key,
        model=model_name,
        reasoning_effort=_env("LLM_REASONING_EFFORT") or None,
    )
    chain = [main]
    # Быстрые и стабильные модели Google с отдельными активными квотами
    auto_fallbacks = ["gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.6-flash"]
    user_fallbacks = [m.strip() for m in _env("LLM_FALLBACK_MODEL", "").split(",") if m.strip()]
    seen = {main.model}
    for fb in user_fallbacks + auto_fallbacks:
        if fb not in seen:
            seen.add(fb)
            chain.append(LLMProvider(main.base_url, main.api_key, fb, main.reasoning_effort))

    # Резервный провайдер (только если не на облаке, где 127.0.0.1 недоступен и вызывает таймаут)
    if reserve_model := _env("RESERVE_MODEL"):
        res_url = _env("RESERVE_BASE_URL", "")
        is_local = "127.0.0.1" in res_url or "localhost" in res_url
        if not (is_local and (os.getenv("PORT") or os.getenv("RENDER"))):
            chain.append(LLMProvider(
                base_url=res_url or None,
                api_key=_env("RESERVE_API_KEY", "none"),
                model=reserve_model,
                reasoning_effort=_env("RESERVE_REASONING_EFFORT") or None,
            ))

    return Settings(
        bot_token=bot_token,
        llm_chain=chain,
        llm_timeout=float(_env("LLM_TIMEOUT", "60")),
        max_input_chars=int(_env("MAX_INPUT_CHARS", "6000")),
        cooldown_seconds=float(_env("COOLDOWN_SECONDS", "5")),
        webapp_port=int(_env("PORT") or _env("WEBAPP_PORT", "8088")),
        webapp_url=(_env("WEBAPP_URL") or _env("RENDER_EXTERNAL_URL") or "").rstrip("/") or None,
    )
