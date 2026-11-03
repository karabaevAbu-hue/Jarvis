"""Голос -> текст: faster-whisper локально (модель small, язык ru; GPU CUDA или CPU int8)."""
from __future__ import annotations

import asyncio
import math
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

# подсказка словаря: короче ошибок на командах
TRADING_PROMPT = ("Консильери, золото, BUY, SELL, стоп, тейк, риск, пауза, робот, закрой сделку, дай зону, "
                  "безубыток, лот, ликвидность, FU, HCS, Order Block, да, нет.")


@dataclass
class STTResult:
    text: str
    confidence: float        # 0..1, по среднему logprob сегментов
    language: str = "ru"


class STT:
    def __init__(self, model_size: str = "small", device: str = "auto", models_dir: str | None = None):
        self.model_size, self.device, self.models_dir = model_size, device, models_dir
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel
        dev, ctype = self.device, "int8"
        if dev == "auto":
            try:
                import ctranslate2
                dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                dev = "cpu"
        if dev == "cuda":
            ctype = "float16"
        self._model = WhisperModel(self.model_size, device=dev, compute_type=ctype, download_root=self.models_dir)
        return self._model

    def _run(self, audio) -> STTResult:
        model = self._load()
        segs, info = model.transcribe(audio, language="ru", beam_size=1, vad_filter=False,
                                      initial_prompt=TRADING_PROMPT, condition_on_previous_text=False)
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs).strip()
        if not segs:
            return STTResult("", 0.0)
        avg = sum(s.avg_logprob for s in segs) / len(segs)
        no_speech = max(s.no_speech_prob for s in segs)
        conf = max(0.0, min(1.0, math.exp(avg))) * (1.0 - min(no_speech, 0.9))
        return STTResult(text, conf)

    async def transcribe_pcm16k(self, pcm: bytes) -> STTResult:
        """Фраза из звонка: PCM s16 моно 16 кГц."""
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(self._run, audio)

    async def transcribe_file(self, path: str | Path) -> STTResult:
        """Голосовое сообщение Telegram (ogg/opus): faster-whisper декодирует файл сам."""
        return await asyncio.to_thread(self._run, str(path))


def write_wav(path: str | Path, pcm: bytes, sr: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
