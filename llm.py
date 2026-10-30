"""Клиент языковой модели. Работает с любым OpenAI-совместимым API
(OpenAI, Gemini, OpenRouter, DeepSeek, локальный OmniRoute и т. д.).

Надёжность: модели перебираются по цепочке (основная → запасная → резервная).
Если одна не ответила или вернула ошибку, запрос сразу уходит следующей,
и пользователь получает ответ, а не ошибку.
"""

import logging
import re
import time

from openai import APIError, AsyncOpenAI

from config import LLMProvider, Settings
from prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)


class AnalysisError(Exception):
    """Ошибка, текст которой можно безопасно показать пользователю."""


class MatchAnalyst:
    def __init__(self, settings: Settings) -> None:
        self._chain: list[tuple[LLMProvider, AsyncOpenAI]] = [
            (p, AsyncOpenAI(
                api_key=p.api_key,
                base_url=p.base_url,
                timeout=min(12.0, settings.llm_timeout),
                max_retries=1,  # 1 попытка повтора при сбое сети
            ))
            for p in settings.llm_chain
        ]

    async def analyze(self, match_data: str, system_prompt: str = SYSTEM_PROMPT) -> str:
        last_err = ""
        for provider, client in list(self._chain):
            started = time.monotonic()
            try:
                text = await self._ask(provider, client, match_data, system_prompt)
                log.info("LLM %s answered in %.1fs", provider.model, time.monotonic() - started)
                # Успешная модель становится первой в очереди для следующих запросов
                pair = (provider, client)
                if pair in self._chain and self._chain[0] != pair:
                    self._chain.remove(pair)
                    self._chain.insert(0, pair)
                return text
            except (APIError, AnalysisError) as e:
                last_err = str(e)
                log.warning("LLM %s failed after %.1fs: %s — trying next",
                            provider.model, time.monotonic() - started, last_err[:200])
                # Сбойная модель уходит в конец очереди
                pair = (provider, client)
                if pair in self._chain and self._chain[-1] != pair:
                    self._chain.remove(pair)
                    self._chain.append(pair)
        raise AnalysisError(f"ИИ временно недоступен ({last_err[:120]}). Попробуйте через минуту.")

    async def analyze_image(self, b64_img: str, caption: str = "", system_prompt: str = SYSTEM_PROMPT) -> str:
        last_err = ""
        prompt_text = caption or "Разбери спортивный матч, коэффициенты и статистику на этом скриншоте:"
        for provider, client in list(self._chain):
            started = time.monotonic()
            try:
                response = await client.chat.completions.create(
                    model=provider.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt_text},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                        ]},
                    ],
                    temperature=0.4,
                    max_tokens=4000,
                )
                text = (response.choices[0].message.content or "").strip() if response.choices else ""
                if text:
                    log.info("LLM %s (vision) answered in %.1fs", provider.model, time.monotonic() - started)
                    pair = (provider, client)
                    if pair in self._chain and self._chain[0] != pair:
                        self._chain.remove(pair)
                        self._chain.insert(0, pair)
                    return sanitize_analysis(text, prompt_text)
            except (APIError, AnalysisError) as e:
                last_err = str(e)
                log.warning("LLM %s (vision) failed after %.1fs: %s — trying next",
                            provider.model, time.monotonic() - started, last_err[:200])
                pair = (provider, client)
                if pair in self._chain and self._chain[-1] != pair:
                    self._chain.remove(pair)
                    self._chain.append(pair)
        raise AnalysisError(f"ИИ временно недоступен ({last_err[:120]}). Попробуйте через минуту.")

    @staticmethod
    async def _ask(provider: LLMProvider, client: AsyncOpenAI, match_data: str, system_prompt: str) -> str:
        # reasoning_effort: передаём только если задан и не 'none'
        extra = {}
        if provider.reasoning_effort and provider.reasoning_effort.lower() != "none":
            extra["reasoning_effort"] = provider.reasoning_effort
        response = await client.chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": match_data},
            ],
            temperature=0.4,  # аналитике нужна стабильность, а не фантазия
            max_tokens=4000,  # с запасом: у «думающих» моделей рассуждение тоже входит в лимит
            **extra,
        )
        text = (response.choices[0].message.content or "").strip() if response.choices else ""
        if not text:
            raise AnalysisError("пустой ответ")
        return sanitize_analysis(text, match_data)


def extract_live_score(match_data: str) -> tuple[int, int] | None:
    """Извлекает текущий счёт матча из карточки, если он там есть."""
    m = re.search(r"Счёт:\s*.*?(\d+)\s*[—–\-:]\s*(\d+)", match_data, re.I)
    if not m:
        m = re.search(r"сч[её]т\s*(\d+)[:\-](\d+)", match_data, re.I)
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except (ValueError, TypeError):
            pass
    return None


def sanitize_totals(text: str) -> str:
    """Исправляет арифметическую ошибку в тоталах, если нейросеть перепутала границу:
    например, написала 'ТБ 10.5 перекрывает сценарий 6:4 или 5:5' (хотя 6+4=10 < 10.5).
    Для перекрытия 10 голов нужен ТБ 9.5.
    """
    pattern = r"(?:ТБ|Тотал больше)\s*(\d+\.5)\b.*?(?:перекрыв\w+|перекро\w+|покрыв\w+|покро\w+|зайд\w+|сыгра\w+|пройд\w+).*?\b(\d+)[:\-](\d+)"
    m = re.search(pattern, text, re.I | re.DOTALL)
    if m:
        tot_val = float(m.group(1))
        s1, s2 = int(m.group(2)), int(m.group(3))
        if s1 + s2 == int(tot_val):
            correct_val = f"{tot_val - 1.0:.1f}".rstrip("0").rstrip(".")
            text = text.replace(f"ТБ {tot_val:.1f}", f"ТБ {correct_val}")
            text = text.replace(f"Тотал больше {tot_val:.1f}", f"Тотал больше {correct_val}")
            text = text.replace(f"тотал больше {tot_val:.1f}", f"тотал больше {correct_val}")
            text = text.replace(f"ТБ {m.group(1)}", f"ТБ {correct_val}")
            text = text.replace(f"Тотал больше {m.group(1)}", f"Тотал больше {correct_val}")
    return text


def sanitize_live_markets(text: str, match_data: str) -> str:
    """Защищает от предложения уже сыгравших или сгоревших маркетов в лайве."""
    sc = extract_live_score(match_data)
    if not sc:
        return text
    h_sc, a_sc = sc
    curr_total = h_sc + a_sc

    # 1. Защита от 'Обе забьют', если обе команды уже забили
    if h_sc >= 1 and a_sc >= 1:
        if re.search(r"Рынок:\s*.*?(?:Обе забьют|ОЗ)\s*[:—–-]?\s*(?:Да|Нет)", text, re.I):
            new_tot = curr_total + 0.5
            text = re.sub(
                r"Рынок:\s*.*?(?:Обе забьют|ОЗ)\s*[:—–-]?\s*(?:Да|Нет)",
                f"Рынок: Тотал больше {new_tot}",
                text,
                flags=re.I,
            )

    # 2. Защита от уже сыгравшего ТБ (когда забито >= тотала)
    m_tb = re.search(r"Рынок:\s*.*?(?:Тотал больше|ТБ)\s*(\d+(?:\.5)?)", text, re.I)
    if m_tb:
        tb_val = float(m_tb.group(1))
        if curr_total >= tb_val:
            new_tot = curr_total + 0.5
            old_str = m_tb.group(1)
            text = text.replace(f"Тотал больше {old_str}", f"Тотал больше {new_tot}")
            text = text.replace(f"ТБ {old_str}", f"ТБ {new_tot}")

    # 3. Защита от уже сгоревшего ТМ (когда забито >= тотала)
    m_tm = re.search(r"Рынок:\s*.*?(?:Тотал меньше|ТМ)\s*(\d+(?:\.5)?)", text, re.I)
    if m_tm:
        tm_val = float(m_tm.group(1))
        if curr_total >= tm_val:
            new_tot = curr_total + 1.5
            old_str = m_tm.group(1)
            text = text.replace(f"Тотал меньше {old_str}", f"Тотал меньше {new_tot}")
            text = text.replace(f"ТМ {old_str}", f"ТМ {new_tot}")

    return text


def sanitize_value_odds(text: str) -> str:
    """Гарантирует +EV: кэф букмекера не может быть ниже справедливого кэфа."""
    m_fair = re.search(r"справедливый\s+коэффициент\s*[:—–-]?\s*(\d+[.,]\d+)", text, re.I)
    m_min = re.search(r"(?:не\s+ниже|не\s+менее|от)\s*[:—–-]?\s*(\d+[.,]\d+)", text, re.I)
    if m_fair and m_min:
        k_fair = float(m_fair.group(1).replace(",", "."))
        k_min = float(m_min.group(1).replace(",", "."))
        if k_min < k_fair:
            new_min = f"{k_fair + 0.05:.2f}"
            text = text.replace(m_min.group(0), m_min.group(0).replace(m_min.group(1), new_min))
    return text


def sanitize_stake_size(text: str) -> str:
    """Удерживает размер флэта в разумных границах банкролл-менеджмента (1–5%)."""
    m = re.search(r"(?:размер\s+ставки|флэт)\s*[:—–-]?\s*(\d+)%", text, re.I)
    if m:
        pct = int(m.group(1))
        if pct > 5:
            text = text.replace(m.group(0), m.group(0).replace(f"{pct}%", "4%"))
        elif pct < 1:
            text = text.replace(m.group(0), m.group(0).replace(f"{pct}%", "1%"))
    return text


def sanitize_analysis(text: str, match_data: str = "") -> str:
    text = strip_markdown(text)
    text = sanitize_totals(text)
    if match_data:
        text = sanitize_live_markets(text, match_data)
    text = sanitize_value_odds(text)
    text = sanitize_stake_size(text)
    return text


def strip_markdown(text: str) -> str:
    """Убирает Markdown-разметку, если модель всё же её добавила:
    бот шлёт обычный текст, и звёздочки/решётки выглядели бы мусором."""
    text = re.sub(r"```+[a-zA-Z]*\n?", "", text)                      # убираем открывающие кодовые блоки
    text = re.sub(r"```+", "", text)                                 # закрывающие кодовые блоки
    text = text.replace("`", "")                                     # убираем инлайн-код
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                     # **жирный**
    text = re.sub(r"^(\s*)[*-]\s+", r"\1• ", text, flags=re.MULTILINE)  # * пункт → • пункт
    text = re.sub(r"\*(.+?)\*", r"\1", text)                         # *курсив*
    text = text.replace("*", "")                                     # любые оставшиеся звездочки
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)       # # заголовки
    text = re.sub(r"^\|?[\s:|-]+\|[\s:|-]*$", "", text, flags=re.MULTILINE)  # разделители таблиц |---|
    return text.strip()
