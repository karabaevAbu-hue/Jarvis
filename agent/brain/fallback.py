"""Ответы без ИИ: когда кончился бюджет API, нет ключа или Claude недоступен (раздел 10 ТЗ).

Статус, сделки дня, зоны, расход, «пауза» / «старт» / «закрыть» по ключевым словам с тем же «да».
Тревоги работают всегда и от этого модуля не зависят.
"""
from __future__ import annotations

import re

from agent.brain.tools import OutMsg, ToolContext, dispatch
from agent.templates import status_text

HELP = ("ИИ сейчас недоступен, отвечаю шаблонами. Могу: статус, сделки, зоны, расход, пауза, старт, закрыть. "
        "Тревоги и торговля робота не затронуты.")

_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(зон|анализ|что по золот|где (покупа|прода))", re.I), "zones"),
    (re.compile(r"(сколько потратил|расход|бюджет)", re.I), "budget"),
    (re.compile(r"(закр(ой|ыть)|выйди из сделк)", re.I), "close_position"),
    (re.compile(r"(пауз|останови робот|не торгуй)", re.I), "pause"),
    (re.compile(r"(старт|возобнов|продолж|сними паузу|снять паузу)", re.I), "resume"),
    (re.compile(r"(сделк|сегодня|итог дня)", re.I), "today"),
    (re.compile(r"(статус|как дела|как робот|робот)", re.I), "status"),
    (re.compile(r"(привет|салам|здравствуй)", re.I), "hello"),
]


def detect(text: str) -> str | None:
    for rx, name in _RULES:
        if rx.search(text or ""):
            return name
    return None


async def handle_without_ai(ctx: ToolContext, text: str, budget_text: str = "") -> list[OutMsg]:
    intent = detect(text)
    if intent == "hello":
        return [OutMsg("Слушаю, брат. " + HELP)]
    if intent == "status":
        return [OutMsg(status_text(ctx.status(), ctx.tz))]
    if intent == "today":
        r = await dispatch(ctx, "get_today", {})
        return [OutMsg(r.get("text", "нет данных"))]
    if intent == "budget":
        return [OutMsg(budget_text or "Бюджет не считается: ИИ не подключён.")]
    if intent == "zones":
        r = await dispatch(ctx, "analyze_zones", {})
        if "error" in r:
            return [OutMsg(r["error"])]
        return list(ctx.outbox)             # текст зон и график уже в outbox
    if intent in ("close_position", "pause", "resume"):
        r = await dispatch(ctx, intent, {})
        return [OutMsg(r.get("final_text", "Не получилось подготовить действие."))]
    return [OutMsg(HELP)]
