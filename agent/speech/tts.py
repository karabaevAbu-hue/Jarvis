"""Текст -> речь: edge-tts (ru-RU-DmitryNeural), кэш частых фраз, ffmpeg для PCM 48 кГц и ogg/opus.

Запасной голос: SAPI Windows через PowerShell; если и он недоступен, говорим только текстом.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("tts")

_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍⭐⬆▶⏹✔✖]+")
_MD = re.compile(r"[*_`#>|]+")
_SUBS = [(r"\bUSD\b", "долларов"), (r"\bUSC\b", "центов"), (r"\bpt\b", "пунктов"), (r"\bRR\b", "эр эр"),
         (r"\bBUY\b", "бай"), (r"\bSELL\b", "селл"), (r"\bTP\b", "тейк"), (r"\bSL\b", "стоп"),
         (r"\bBE\b", "безубыток"), (r"\bMT5\b", "эм ти пять"), (r"\bH1\b", "аш один"), (r"\bH4\b", "аш четыре"),
         (r"\bM15\b", "эм пятнадцать"), (r"\bD1\b", "дэ один"), (r"−", "минус "), (r"%", " процентов")]


def speakable(text: str) -> str:
    """Убирает эмодзи и разметку, раскрывает сокращения, чтобы голос не читал мусор."""
    t = _EMOJI.sub(" ", text)
    t = _MD.sub(" ", t)
    for rx, rep in _SUBS:
        t = re.sub(rx, rep, t)
    return re.sub(r"\s+", " ", t).strip()


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """Режет по предложениям: озвучка идёт кусками, первый кусок звучит раньше всего ответа."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(",", 0, max_len)
            cut = cut if cut > 40 else p.rfind(" ", 0, max_len)
            out.append(p[:cut].strip())
            p = p[cut:].lstrip(", ")
        out.append(p)
    return out


class TTS:
    def __init__(self, voice: str = "ru-RU-DmitryNeural", cache_dir: str | Path = "data/cache/tts"):
        self.voice = voice
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def _key(self, text: str) -> Path:
        return self.cache / (hashlib.sha1(f"{self.voice}|{text}".encode()).hexdigest() + ".mp3")

    async def _synth_edge(self, text: str) -> bytes:
        import edge_tts
        chunks = bytearray()
        async for ch in edge_tts.Communicate(text, self.voice).stream():
            if ch["type"] == "audio":
                chunks += ch["data"]
        if not chunks:
            raise RuntimeError("edge-tts вернул пустой звук")
        return bytes(chunks)

    async def _synth_sapi(self, text: str) -> bytes:
        """Голос Windows (запасной). Возвращает WAV."""
        if not sys.platform.startswith("win"):
            raise RuntimeError("SAPI есть только на Windows")
        out = self.cache / "sapi.wav"
        ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$s.SetOutputToWaveFile('{out}'); $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
        p = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-Command", ps,
                                                 stdin=asyncio.subprocess.PIPE)
        await p.communicate(text.encode("utf-8"))
        return out.read_bytes()

    async def synth(self, text: str) -> bytes | None:
        """Звук (mp3 или wav) для текста или None, если озвучить нечем. Частые фразы берутся из кэша."""
        text = speakable(text)
        if not text:
            return None
        f = self._key(text)
        if f.exists():
            return f.read_bytes()
        try:
            data = await self._synth_edge(text)
            f.write_bytes(data)
            return data
        except Exception as e:
            log.warning("edge-tts недоступен (%s), пробую голос Windows", e)
        try:
            return await self._synth_sapi(text)
        except Exception as e:
            log.warning("озвучить не получилось (%s): только текст", e)
            return None

    async def _ffmpeg(self, data: bytes, args: list[str]) -> bytes:
        p = await asyncio.create_subprocess_exec("ffmpeg", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1",
                                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate(data)
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg: {err.decode(errors='replace')[:200]}")
        return out

    async def pcm48k(self, text: str) -> bytes | None:
        """Звук для звонка: PCM s16 48 кГц моно."""
        audio = await self.synth(text)
        return None if audio is None else await self._ffmpeg(audio, ["-f", "s16le", "-ar", "48000", "-ac", "1"])

    async def ogg_opus(self, text: str) -> bytes | None:
        """Голосовое сообщение Telegram: ogg/opus."""
        audio = await self.synth(text)
        return None if audio is None else await self._ffmpeg(audio, ["-c:a", "libopus", "-b:a", "32k", "-f", "ogg"])
