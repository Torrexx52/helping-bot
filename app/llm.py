"""ИИ через любой OpenAI-совместимый API: JSON-ответ, таймаут, дневной лимит (spec-leads §5).

Любая неудача (нет ключа, лимит, таймаут, ошибка API, кривой ответ) — исключение LLMUnavailable:
вызывающий код работает дальше «без проверки», бот не падает.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import httpx

from app.storage import Storage

log = logging.getLogger(__name__)

LEAD_SYSTEM = (
    "Ты фильтр сообщений из чатов для сервиса грузоперевозок и переездов в Воронеже. "
    "Отвечай строго одним JSON-объектом, без пояснений."
)
LEAD_QUESTION = (
    "Это человек, которому нужна перевозка, переезд, грузчики или вывоз (мебели, техники, мусора) "
    "в Воронеже или области? Объявления тех, кто сам предлагает такие услуги, — не заявка.\n"
    'Ответ строго JSON: {"lead": true|false, "summary": "..."}. '
    "Если да — в summary одна строка: что, откуда-куда, когда (только то, что есть в сообщении). "
    "Если нет — summary пустой.\n\nСообщение:\n"
)


class LLMUnavailable(Exception):
    """ИИ недоступен; reason — короткая причина для логов."""


def extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (допускает ```json-обёртку и текст вокруг)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("в ответе нет JSON")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise ValueError(f"не JSON: {e.msg}") from e
    if not isinstance(obj, dict):
        raise ValueError("JSON не объект")
    return obj


def parse_lead_answer(text: str) -> tuple[bool, str]:
    """Разбор ответа на вопрос о заявке: (lead, summary). ValueError — если ответ не по формату."""
    obj = extract_json(text)
    lead = obj.get("lead")
    if isinstance(lead, str) and lead.strip().lower() in ("true", "false"):
        lead = lead.strip().lower() == "true"
    if not isinstance(lead, bool):
        raise ValueError("нет поля lead")
    summary = obj.get("summary")
    summary = " ".join(summary.split())[:300] if isinstance(summary, str) else ""
    return lead, summary


class LLM:
    def __init__(
        self,
        storage: Storage,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 20,
        daily_limit: int = 300,
        tz: str = "Europe/Moscow",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.storage = storage
        self.url = base_url.rstrip("/") + "/chat/completions" if base_url else ""
        self.api_key = api_key
        self.model = model
        self.daily_limit = daily_limit
        self.tz = ZoneInfo(tz)
        self._json_mode = True
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key and self.model)

    def _key(self) -> str:
        return "llm:" + dt.datetime.now(self.tz).date().isoformat()

    async def used_today(self) -> int:
        return int(await self.storage.kv_get(self._key(), "0"))

    async def remaining(self) -> int:
        if not self.configured:
            return 0
        return max(0, self.daily_limit - await self.used_today())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(self, system: str, user: str, reserve: int = 0) -> str:
        """Один вызов чата. reserve — сколько вызовов оставить на сегодня другим задачам."""
        if not self.configured:
            raise LLMUnavailable("ИИ не настроен")
        if await self.remaining() <= reserve:
            raise LLMUnavailable("дневной лимит ИИ исчерпан")
        key = self._key()
        await self.storage.kv_incr(key)
        await self.storage.kv_delete_prefix("llm:", keep=key)
        payload: dict = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = await self._post(payload)
            return resp.json()["choices"][0]["message"]["content"] or ""
        except httpx.TimeoutException as e:
            raise LLMUnavailable("таймаут ИИ") from e
        except httpx.HTTPStatusError as e:
            raise LLMUnavailable(f"ИИ ответил HTTP {e.response.status_code}") from e
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMUnavailable(f"ошибка ИИ: {type(e).__name__}") from e

    async def _post(self, payload: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self._json_mode:
            resp = await self._client.post(
                self.url, headers=headers, json={**payload, "response_format": {"type": "json_object"}}
            )
            if resp.status_code not in (400, 404, 422):
                resp.raise_for_status()
                return resp
            self._json_mode = False  # провайдер не знает response_format — дальше без него
        resp = await self._client.post(self.url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp

    async def ask_json(self, system: str, user: str, reserve: int = 0) -> dict:
        try:
            return extract_json(await self.ask(system, user, reserve))
        except ValueError as e:
            raise LLMUnavailable(f"ИИ ответил не по формату: {e}") from e

    async def classify_lead(self, text: str) -> tuple[bool, str]:
        """(lead, summary). LLMUnavailable — прислать сообщение «без проверки»."""
        try:
            return parse_lead_answer(await self.ask(LEAD_SYSTEM, LEAD_QUESTION + text[:1500]))
        except ValueError as e:
            raise LLMUnavailable(f"ИИ ответил не по формату: {e}") from e
