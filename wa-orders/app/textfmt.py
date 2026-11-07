"""Сводка заказов и перевод разметки Telegram → WhatsApp."""
from __future__ import annotations

import html
import random
import re

NUMBERS = ("1️⃣", "2️⃣", "3️⃣")

_TAGS = [
    (re.compile(r"</?(?:b|strong)>"), "*"),
    (re.compile(r"</?(?:i|em)>"), "_"),
    (re.compile(r"</?(?:s|strike|del)>"), "~"),
]
_ANY_TAG = re.compile(r"<[^>]+>")


def tg_html_to_wa(text: str) -> str:
    """aiogram отдаёт message.html_text; WhatsApp понимает *жирный*, _курсив_, ~зачёркнутый~."""
    for pattern, mark in _TAGS:
        text = pattern.sub(mark, text)
    text = _ANY_TAG.sub("", text)
    return html.unescape(text).strip()


def build_summary(orders: list[str], header: str = "", footer: str = "") -> str:
    parts: list[str] = []
    if header.strip():
        parts.append(f"*{header.strip()}*")
    for number, order in zip(NUMBERS, orders):
        parts.append(f"{number} {order.strip()}")
    if footer.strip():
        parts.append(footer.strip())
    return "\n\n".join(parts)


def pick_header(headers: list[str], last_index: int | None, rng: random.Random) -> tuple[int | None, str]:
    """Выбирает шапку, отличную от прошлой в этой группе (если вариантов больше одного)."""
    options = [(i, h) for i, h in enumerate(headers, start=1) if h.strip()]
    if not options:
        return None, ""
    if len(options) > 1:
        options = [o for o in options if o[0] != last_index] or options
    return rng.choice(options)


def split_orders(text: str) -> list[str]:
    """«Вставить все три»: заказы разделены пустой строкой."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return blocks
