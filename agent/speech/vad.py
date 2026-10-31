"""Конец фразы: VAD + эндпойнтинг (пауза ~0.7 с), перебивание, ресемплинг 48 -> 16 кГц.

Логика эндпойнтера не зависит от модели VAD: ему нужен только флаг «речь / не речь» на кадр.
EnergyVAD работает на чистом numpy (и в тестах), SileroVAD точнее и подключается, если стоит torch.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SR_CALL = 48000          # звук звонка Telegram: PCM 48 кГц, моно, s16
SR_STT = 16000           # Whisper и Silero
FRAME_MS = 32            # 512 отсчётов при 16 кГц: столько ждёт Silero


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def resample_48k_to_16k(pcm: bytes) -> bytes:
    """Прореживание в 3 раза с усреднением (простой ФНЧ). Для речи достаточно."""
    x = np.frombuffer(pcm, dtype=np.int16)
    n = len(x) // 3 * 3
    if n == 0:
        return b""
    return x[:n].reshape(-1, 3).astype(np.int32).mean(axis=1).astype(np.int16).tobytes()


class EnergyVAD:
    """Речь по энергии кадра с адаптивным порогом над шумом."""

    def __init__(self, threshold_db: float = 12.0, floor_init: float = 0.003):
        self.floor = floor_init
        self.threshold = 10 ** (threshold_db / 20.0)

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame ** 2))) if len(frame) else 0.0
        speech = rms > max(self.floor * self.threshold, 0.01)
        if not speech:                                      # шум подстраивает пол
            self.floor = 0.95 * self.floor + 0.05 * max(rms, 1e-4)
        return speech


class SileroVAD:
    """Silero VAD (нужен torch). Принимает кадры по 512 отсчётов 16 кГц."""

    def __init__(self, threshold: float = 0.5):
        import torch
        from silero_vad import load_silero_vad
        self.torch = torch
        self.model = load_silero_vad()
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if len(frame) != 512:
            return False
        p = self.model(self.torch.from_numpy(frame), SR_STT).item()
        return p >= self.threshold

    def reset(self) -> None:
        self.model.reset_states()


@dataclass
class Utterance:
    pcm16k: bytes
    duration_s: float


@dataclass
class Endpointer:
    """Склеивает кадры в фразы: старт речи, конец после `end_silence_ms` тишины.

    feed(pcm48k) возвращает список событий: ("start", None) при начале речи (по нему перебивание),
    ("end", Utterance) при конце фразы.
    """
    vad: object
    end_silence_ms: int = 700
    min_speech_ms: int = 250
    max_utt_s: float = 30.0
    pre_roll_ms: int = 160
    _buf: bytearray = field(default_factory=bytearray)
    _tail: bytes = b""
    _in_speech: bool = False
    _speech_ms: int = 0
    _silence_ms: int = 0
    _total_ms: int = 0
    _pre: list = field(default_factory=list)

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def reset(self) -> None:
        self._buf.clear()
        self._tail = b""
        self._in_speech = False
        self._speech_ms = self._silence_ms = self._total_ms = 0
        self._pre.clear()

    def feed(self, pcm48k: bytes) -> list[tuple[str, Utterance | None]]:
        events: list[tuple[str, Utterance | None]] = []
        data = self._tail + resample_48k_to_16k(pcm48k)
        step = SR_STT * FRAME_MS // 1000 * 2                    # байт на кадр
        pos = 0
        while pos + step <= len(data):
            frame = data[pos:pos + step]
            pos += step
            speech = self.vad.is_speech(pcm_to_float(frame))
            if speech:
                if not self._in_speech:
                    self._in_speech = True
                    self._buf = bytearray(b"".join(self._pre))
                    self._speech_ms = 0
                    events.append(("start", None))
                self._buf += frame
                self._speech_ms += FRAME_MS
                self._silence_ms = 0
            elif self._in_speech:
                self._buf += frame
                self._silence_ms += FRAME_MS
                self._total_ms += FRAME_MS
                if self._silence_ms >= self.end_silence_ms:
                    events.extend(self._finish())
            else:
                self._pre.append(frame)
                self._pre = self._pre[-max(1, self.pre_roll_ms // FRAME_MS):]
            if self._in_speech:
                self._total_ms = len(self._buf) * 1000 // (SR_STT * 2)
                if self._total_ms >= self.max_utt_s * 1000:
                    events.extend(self._finish())
        self._tail = data[pos:]
        return events

    def _finish(self) -> list[tuple[str, Utterance | None]]:
        pcm = bytes(self._buf)
        spoke = self._speech_ms
        self._buf.clear()
        self._in_speech = False
        self._silence_ms = self._speech_ms = self._total_ms = 0
        self._pre.clear()
        if spoke < self.min_speech_ms:            # щелчок или кашель, не фраза
            return []
        return [("end", Utterance(pcm, len(pcm) / (SR_STT * 2)))]


def make_vad(prefer_silero: bool = True):
    """Silero, если доступен, иначе энергетический VAD."""
    if prefer_silero:
        try:
            return SileroVAD()
        except Exception:
            pass
    return EnergyVAD()
