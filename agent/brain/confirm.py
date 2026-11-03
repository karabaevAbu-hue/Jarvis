"""Подтверждение «да» зашито в код (раздел 10 ТЗ). Модель сама ничего не выполняет.

Правила:
 1. Инструмент на действие только создаёт ожидающее действие; текст с точными цифрами пишет код.
 2. Выполняется, только если СЛЕДУЮЩЕЕ сообщение владельца — явное согласие в течение 60 с.
    Любое другое слово или тишина = отмена.
 3. Одно действие за раз; новое предложение заменяет старое; «да» одноразовое и не переносится.
 4. Пересланное сообщение и текст от модели согласием не считаются.
 5. Голос с низкой уверенностью распознавания: переспросить, а не выполнять.
 6. Исключение: emergency_pause без «да», только при открытой критичной тревоге.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from agent.storage.db import Database

TTL_SEC = 60
MIN_VOICE_CONFIDENCE = 0.6

AFFIRM = {"да", "давай", "подтверждаю", "ha", "ха"}
FILLER = {"брат", "абу", "консильери", "пожалуйста"}
NEGATIVE = {"нет", "не", "отмена", "отменить", "стоп", "cancel", "yoq", "йок", "no"}

_WORD = re.compile(r"[a-zа-яёқғўҳ]+", re.IGNORECASE)


def words(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in _WORD.findall(text or "")]


def is_affirmative(text: str) -> bool:
    """Согласие: только слова из AFFIRM (+ вежливые «брат», «Абу»), без отрицаний и посторонних слов."""
    ws = [w for w in words(text) if w not in FILLER]
    if not ws:
        return False
    norm = {w.replace("ё", "е") for w in AFFIRM}
    return all(w in norm for w in ws)


def is_negative(text: str) -> bool:
    return any(w in NEGATIVE for w in words(text))


@dataclass
class ExecResult:
    ok: bool
    msg: str


Executor = Callable[[str, dict], Awaitable[ExecResult]]


@dataclass
class Pending:
    id: str
    kind: str
    args: dict
    prompt: str
    created: float
    expires: float
    reasked: bool = False


class Kind(str, Enum):
    NONE = "none"            # нечего подтверждать
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    REASK = "reask"
    EXPIRED = "expired"


@dataclass
class Outcome:
    kind: Kind
    text: str = ""
    consumed: bool = False   # True: сообщение обработано здесь, дальше его не разбирать


class ConfirmManager:
    def __init__(self, db: Database, executor: Executor, ttl: float = TTL_SEC,
                 clock: Callable[[], float] = time.time, who: str = "Абу"):
        self.db = db
        self.executor = executor
        self.ttl = ttl
        self.clock = clock
        self.who = who
        self.pending: Pending | None = None

    # ---------- предложение действия ----------
    async def propose(self, kind: str, args: dict, prompt: str) -> Pending:
        """Создаёт ожидающее действие. Прежнее (если было) отменяется: одно действие за раз."""
        if self.pending is not None:
            await self.db.pending_set_status(self.pending.id, "superseded")
        now = self.clock()
        p = Pending(uuid.uuid4().hex[:12], kind, dict(args), prompt, now, now + self.ttl)
        await self.db.pending_put(p.id, kind, args, prompt, p.created, p.expires)
        self.pending = p
        return p

    # ---------- каждое входящее сообщение владельца проходит сюда ----------
    async def handle(self, text: str, *, forwarded: bool = False, confidence: float = 1.0) -> Outcome:
        p = self.pending
        if p is None:
            return Outcome(Kind.NONE)
        now = self.clock()
        if now > p.expires:
            self.pending = None
            await self.db.pending_set_status(p.id, "expired")
            if is_affirmative(text) and not forwarded:
                return Outcome(Kind.EXPIRED, f"Прошло больше {self.ttl:g} с: действие не выполнено. "
                               "Скажи, что нужно, и я спрошу заново.", consumed=True)
            return Outcome(Kind.EXPIRED)                      # другое сообщение разбираем как обычно
        if forwarded:
            self.pending = None
            await self.db.pending_set_status(p.id, "cancelled")
            await self.db.log_action(p.kind, p.args, self.who, text, "отмена: пересланное сообщение")
            return Outcome(Kind.CANCELLED)
        if is_affirmative(text):
            if confidence < MIN_VOICE_CONFIDENCE and not p.reasked:
                p.reasked = True
                return Outcome(Kind.REASK, f"Не уверен, что расслышал. {p.prompt}", consumed=True)
            # одноразовость: статус pending -> done меняется один раз
            won = await self.db.pending_set_status(p.id, "done", only_if="pending")
            self.pending = None
            if not won:
                return Outcome(Kind.CANCELLED, "Это действие уже закрыто.", consumed=True)
            try:
                res = await self.executor(p.kind, p.args)
            except Exception as e:  # исполнитель не должен ронять диалог
                res = ExecResult(False, f"ошибка: {e}")
            await self.db.log_action(p.kind, p.args, self.who, text, ("ok: " if res.ok else "fail: ") + res.msg)
            return Outcome(Kind.EXECUTED, ("✔️ Готово: " if res.ok else "✖️ Не выполнено: ") + res.msg, consumed=True)
        # любое другое слово = отмена
        self.pending = None
        await self.db.pending_set_status(p.id, "cancelled")
        await self.db.log_action(p.kind, p.args, self.who, text, "отмена")
        if is_negative(text):
            return Outcome(Kind.CANCELLED, "Отменил. Ничего не менял.", consumed=True)
        return Outcome(Kind.CANCELLED)                          # сообщение разбираем как новый запрос

    def has_pending(self) -> bool:
        return self.pending is not None and self.clock() <= self.pending.expires

    # ---------- единственное исключение ----------
    async def emergency_pause(self, reason: str) -> ExecResult:
        """Пауза без «да»: вызывается кодом только при открытой критичной тревоге."""
        try:
            res = await self.executor("pause", {})
        except Exception as e:
            res = ExecResult(False, f"ошибка: {e}")
        await self.db.log_action("emergency_pause", {"reason": reason}, "система", None,
                                 ("ok: " if res.ok else "fail: ") + res.msg)
        return res
