"""Клиент Claude: инструменты, кэш инструкции, подсчёт расхода, дневной бюджет (раздел 10 и 15 ТЗ).

Две модели: MODEL_FAST (чат, звонки) и MODEL_SMART («дай зону», разборы). Инструкция (характер,
стратегия, книга) кэшируется; динамическая сводка идёт отдельным блоком после точки кэша.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from agent.config import Settings
from agent.storage.db import Database
from agent.util import local_day, utcnow

log = logging.getLogger("llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WARN_AT = 0.8


class LLMUnavailable(Exception):
    """Сеть, ключ, лимиты или 5xx: ассистент переходит на ответы без ИИ."""


class BudgetExhausted(Exception):
    """Дневной бюджет исчерпан: до полуночи только шаблоны без ИИ."""


@dataclass
class BudgetState:
    used: float
    limit: float
    calls: int = 0

    @property
    def pct(self) -> float:
        return self.used / self.limit if self.limit > 0 else 1.0

    @property
    def level(self) -> str:
        return "blocked" if self.pct >= 1.0 else "warn" if self.pct >= WARN_AT else "ok"

    def text(self) -> str:
        return f"Сегодня на ИИ потрачено {self.used:.3f} из {self.limit:.2f} USD ({self.pct * 100:.0f}%), запросов {self.calls}."


class Budget:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self.db, self.s, self.clock = db, settings, clock

    def day(self) -> str:
        return local_day(self.clock(), self.s.zone)

    async def state(self) -> BudgetState:
        sm = await self.db.usage_summary(self.day())
        return BudgetState(float(sm["cost"]), self.s.daily_budget_usd, int(sm["n"]))

    async def check(self) -> BudgetState:
        st = await self.state()
        if st.level == "blocked":
            raise BudgetExhausted(st.text())
        return st

    async def warn_once(self) -> str | None:
        """Текст предупреждения при первом пересечении 80% за день, иначе None."""
        st = await self.state()
        if st.level == "ok":
            return None
        key = f"budget.warned.{self.day()}.{st.level}"
        if await self.db.kv_get(key):
            return None
        await self.db.kv_set(key, True)
        if st.level == "blocked":
            return (f"⚠️ Дневной бюджет ИИ исчерпан ({st.used:.2f} из {st.limit:.2f} USD). До полуночи отвечаю "
                    "шаблонами: статус, сделки, зоны, пауза, старт, закрыть. Тревоги работают как обычно.")
        return f"⚠️ Израсходовано {st.pct * 100:.0f}% дневного бюджета ИИ ({st.used:.2f} из {st.limit:.2f} USD)."

    def prices(self, kind: str) -> tuple[float, float]:
        return (self.s.price_in_fast, self.s.price_out_fast) if kind == "fast" else \
               (self.s.price_in_smart, self.s.price_out_smart)

    def cost_of(self, kind: str, usage: Any) -> tuple[float, int, int, int, int]:
        pin, pout = self.prices(kind)
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = (tin * pin + tout * pout + cw * pin * self.s.cache_write_mult + cr * pin * self.s.cache_read_mult) / 1e6
        return cost, tin, tout, cw, cr

    async def record(self, kind: str, model: str, usage: Any) -> float:
        cost, tin, tout, cw, cr = self.cost_of(kind, usage)
        await self.db.add_usage(self.day(), model, tin, tout, cw, cr, cost)
        return cost


@dataclass
class TurnResult:
    text: str
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    cost: float = 0.0
    model: str = ""
    stop_reason: str = ""
    forced: bool = False          # текст задан кодом (вопрос «да / нет»), а не моделью
    refused: bool = False


Dispatcher = Callable[[str, dict], Awaitable[dict]]


class LLM:
    def __init__(self, settings: Settings, db: Database, client: Any = None,
                 clock: Callable[[], datetime] = utcnow):
        self.s = settings
        self.db = db
        self.budget = Budget(db, settings, clock)
        self._client = client
        self._fallback_ok = settings.smart_server_fallback

    @property
    def available(self) -> bool:
        return bool(self._client) or bool(self.s.anthropic_api_key)

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key, max_retries=2, timeout=60.0)
        return self._client

    def model_for(self, kind: str) -> str:
        return self.s.model_fast if kind == "fast" else self.s.model_smart

    async def _create(self, kind: str, **kw):
        import anthropic
        c = self.client
        try:
            if kind == "smart" and self._fallback_ok:
                try:
                    return await c.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
                except anthropic.BadRequestError as e:
                    # платформа не принимает fallbacks: работаем без него, но не молча
                    log.warning("fallbacks отклонён, отключаю: %s", e)
                    self._fallback_ok = False
            return await c.messages.create(**kw)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
                anthropic.InternalServerError) as e:
            raise LLMUnavailable(f"{type(e).__name__}") from e
        except anthropic.AuthenticationError as e:
            raise LLMUnavailable("ключ API не подходит") from e
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", 0) >= 500 or getattr(e, "status_code", 0) in (408, 429, 529):
                raise LLMUnavailable(f"API {e.status_code}") from e
            raise

    @staticmethod
    def _tool_result_content(res: dict, allow_images: bool):
        body = {k: v for k, v in res.items() if not k.startswith("_") and k != "final_text"}
        text = json.dumps(body, ensure_ascii=False, default=str)
        png = res.get("_image_png")
        if png and allow_images:
            return [{"type": "text", "text": text},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.standard_b64encode(png).decode()}}]
        return text

    async def run_turn(self, *, kind: str, system_static: str, system_dynamic: str, messages: list[dict],
                       tools: list[dict], dispatch: Dispatcher, max_iters: int = 6,
                       max_tokens: int | None = None, allow_images: bool | None = None) -> TurnResult:
        await self.budget.check()
        model = self.model_for(kind)
        allow_images = (kind == "smart") if allow_images is None else allow_images
        max_tokens = max_tokens or (2048 if kind == "fast" else 6000)
        effort = "low" if kind == "fast" else "medium"
        system = [{"type": "text", "text": system_static, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        if system_dynamic:
            system.append({"type": "text", "text": system_dynamic})
        msgs = list(messages)
        result = TurnResult("", model=model)

        for _ in range(max_iters):
            kw: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=system, messages=msgs,
                                      output_config={"effort": effort})
            if tools:
                kw["tools"] = tools
            resp = await self._create(kind, **kw)
            result.cost += await self.budget.record(kind, model, resp.usage)
            result.stop_reason = resp.stop_reason or ""
            if resp.stop_reason == "refusal":
                result.text, result.refused = "Не могу ответить на этот запрос.", True
                return result
            texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                result.text = "\n".join(t for t in texts if t).strip()
                if resp.stop_reason == "max_tokens":
                    result.text += "\n(ответ оборван по длине)"
                return result

            msgs.append({"role": "assistant", "content": resp.content})
            outs = await asyncio.gather(*[dispatch(u.name, dict(u.input or {})) for u in uses])
            blocks = []
            finals: list[str] = []
            for u, out in zip(uses, outs):
                result.tool_calls.append((u.name, dict(u.input or {})))
                blocks.append({"type": "tool_result", "tool_use_id": u.id,
                               "content": self._tool_result_content(out, allow_images),
                               **({"is_error": True} if "error" in out else {})})
                if out.get("final_text"):
                    finals.append(out["final_text"])
            msgs.append({"role": "user", "content": blocks})
            if finals:                                   # вопрос «да / нет» формулирует код, не модель
                result.text, result.forced = "\n".join(finals), True
                return result
        result.text = "Слишком длинная цепочка вызовов, остановился. Переспроси короче."
        return result
