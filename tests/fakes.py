"""Двойники для тестов: «Claude» по сценарию и цикл робота."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any, Callable


def usage(i=1000, o=100, cw=0, cr=0):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


def text_resp(text: str, u=None, stop="end_turn"):
    return NS(content=[NS(type="text", text=text)], stop_reason=stop, usage=u or usage())


def tool_resp(name: str, inp: dict | None = None, tid: str = "tu_1", u=None, pre_text: str = ""):
    blocks = ([NS(type="text", text=pre_text)] if pre_text else []) + [NS(type="tool_use", id=tid, name=name, input=inp or {})]
    return NS(content=blocks, stop_reason="tool_use", usage=u or usage())


class _Messages:
    def __init__(self, owner: "FakeClaude", beta: bool):
        self.owner, self.beta = owner, beta

    async def create(self, **kw):
        self.owner.calls.append({"beta": self.beta, **kw})
        if not self.owner.script:
            raise AssertionError("сценарий FakeClaude закончился, а модель вызвали снова")
        item = self.owner.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kw) if callable(item) else item


class FakeClaude:
    """messages.create отдаёт заранее заданные ответы по порядку и запоминает вызовы."""

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.messages = _Messages(self, beta=False)
        self.beta = NS(messages=_Messages(self, beta=True))


async def robot_loop(ea, every: float = 0.03):
    """Робот отвечает на команды из папки cmd, пока задача жива."""
    while True:
        ea.process_commands()
        await asyncio.sleep(every)
