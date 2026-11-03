"""Голос: конец фразы по паузе 0.7 с, перебивание, ресемплинг, очистка текста для озвучки, кэш TTS."""
import io
import subprocess
import wave

import numpy as np
import pytest

from agent.speech.tts import TTS, speakable, split_sentences
from agent.speech.vad import SR_CALL, EnergyVAD, Endpointer, float_to_pcm, resample_48k_to_16k


def tone(sec, freq=220.0, amp=0.4):
    t = np.arange(int(SR_CALL * sec)) / SR_CALL
    return float_to_pcm(amp * np.sin(2 * np.pi * freq * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)))


def silence(sec):
    return float_to_pcm(np.random.default_rng(1).normal(0, 0.002, int(SR_CALL * sec)))


def feed_in_chunks(ep, pcm, chunk_ms=20):
    step = SR_CALL * chunk_ms // 1000 * 2
    ev = []
    for i in range(0, len(pcm), step):
        ev += ep.feed(pcm[i:i + step])
    return ev


def mk():
    return Endpointer(EnergyVAD())


def test_resample_ratio():
    pcm = tone(1.0)
    assert len(resample_48k_to_16k(pcm)) == len(pcm) // 3


def test_silence_gives_no_events():
    assert feed_in_chunks(mk(), silence(2.0)) == []


def test_phrase_ends_after_700ms_pause():
    ep = mk()
    ev = feed_in_chunks(ep, silence(0.3) + tone(1.2) + silence(0.5))
    assert [e[0] for e in ev] == ["start"]                    # 0.5 с тишины мало: фраза ещё идёт
    ev2 = feed_in_chunks(ep, silence(0.4))
    assert [e[0] for e in ev2] == ["end"]                      # суммарно 0.9 с >= 0.7 с
    utt = ev2[0][1]
    assert 1.1 <= utt.duration_s <= 2.2


def test_click_is_not_a_phrase():
    ev = feed_in_chunks(mk(), silence(0.3) + tone(0.1) + silence(1.0))
    assert "end" not in [e[0] for e in ev]


def test_two_phrases_are_separate():
    ev = feed_in_chunks(mk(), tone(0.8) + silence(1.0) + tone(0.8) + silence(1.0))
    assert [e[0] for e in ev] == ["start", "end", "start", "end"]


def test_barge_in_signal_arrives_on_start():
    ep = mk()
    feed_in_chunks(ep, silence(0.5))
    ev = feed_in_chunks(ep, tone(0.3))
    assert ev and ev[0][0] == "start" and ep.in_speech          # по «start» ассистент замолкает


def test_long_monologue_is_cut():
    ep = Endpointer(EnergyVAD(), max_utt_s=2.0)
    ev = feed_in_chunks(ep, tone(5.0))
    assert any(e[0] == "end" for e in ev)


# ---------------------------------------------------------------- озвучка
def test_speakable_strips_emoji_and_expands_units():
    t = speakable("🟢 BUY 2641.5–2645.0 · SL 2639 · риск 11 USD (1.1%) **важно**")
    assert "🟢" not in t and "*" not in t and "бай" in t and "долларов" in t and "стоп" in t


def test_split_sentences_streams_first_chunk_early():
    parts = split_sentences("Работает. Сегодня две сделки! День плюс восемнадцать долларов? Да.")
    assert parts[0] == "Работает." and len(parts) == 4
    long = split_sentences("слово, " * 80)
    assert all(len(p) <= 230 for p in long)


def _wav(sec=0.5, sr=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.sin(np.arange(int(sr * sec)) * 0.1) * 8000).astype(np.int16).tobytes())
    return buf.getvalue()


async def test_tts_cache_hits_and_text_fallback(tmp_path, monkeypatch):
    tts = TTS(cache_dir=tmp_path)
    calls = []

    async def fake_edge(self, text):
        calls.append(text)
        return _wav()
    monkeypatch.setattr(TTS, "_synth_edge", fake_edge)
    a = await tts.synth("Слушаю, Абу")
    b = await tts.synth("Слушаю, Абу")
    assert a == b and len(calls) == 1                           # вторая фраза из кэша
    assert await tts.synth("🟢 ") is None                        # озвучивать нечего

    async def broken(self, text):
        raise RuntimeError("нет сети")
    monkeypatch.setattr(TTS, "_synth_edge", broken)
    assert await TTS(cache_dir=tmp_path / "x").synth("новая фраза") is None   # честно: только текстом


async def test_tts_to_pcm48k_via_ffmpeg(tmp_path, monkeypatch):
    async def fake_edge(self, text):
        return _wav(0.5)
    monkeypatch.setattr(TTS, "_synth_edge", fake_edge)
    pcm = await TTS(cache_dir=tmp_path).pcm48k("проверка")
    assert abs(len(pcm) / 2 / 48000 - 0.5) < 0.1


@pytest.mark.skipif("libopus" not in subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True).stdout,
                    reason="в ffmpeg нет libopus")
async def test_tts_to_ogg_opus(tmp_path, monkeypatch):
    async def fake_edge(self, text):
        return _wav(0.5)
    monkeypatch.setattr(TTS, "_synth_edge", fake_edge)
    ogg = await TTS(cache_dir=tmp_path).ogg_opus("проверка")
    assert ogg[:4] == b"OggS"
