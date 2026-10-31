"""tools/parity.py: выгрузка -> порт -> сравнение со status.json робота."""
import json
from datetime import timezone
from pathlib import Path

import pandas as pd
import pytest

from agent.analysis.engine import AnalysisEngine
from tools import mql_lint, parity
from tools.fake_mt5 import FakeMT5Source, synthetic_m5

ROOT = Path(__file__).resolve().parent.parent
BIAS = {"BUY": "BUY", "SELL": "SELL", "NEUTRAL": "NONE"}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    """Свечи в том виде, как их пишет скрипт MrCasinoExport (2 знака, epoch UTC), и status.json из зон порта."""
    base = tmp_path_factory.mktemp("mc")
    pdir = base / "parity"
    pdir.mkdir()
    src = FakeMT5Source(synthetic_m5(weeks=110, seed=21).round(2))
    raw = {}
    for tf, n in (("M5", 900), ("M15", 700), ("H1", 200), ("H4", 100), ("D1", 60)):
        df = src.candles("X", tf, n).round(2)
        raw[tf] = df
        out = df.copy()
        out.insert(0, "time", [int(t.timestamp()) for t in out.index])
        out.to_csv(pdir / f"{tf}.csv", index=False, float_format="%.2f")
    last = raw["M5"].index[-1]
    now = int(last.timestamp()) + 150
    bid = float(raw["M5"]["close"].iloc[-1])
    (pdir / "meta.json").write_text(json.dumps({"symbol": "XAUUSDm", "digits": 2, "bid": bid, "ask": round(bid + 0.2, 2),
                                                "now_utc": now, "server_offset_sec": 0}), encoding="utf-8")
    eng = AnalysisEngine(None)  # type: ignore[arg-type]
    from datetime import datetime
    when = datetime.fromtimestamp(now, tz=timezone.utc)
    res = eng.analyze_frames(raw, "XAUUSDm", {"bid": bid, "ask": bid + 0.2, "time": when}, now=when)
    status = {"ts_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "bias": {"d1": BIAS[res.bias["d1"]], "h4": BIAS[res.bias["h4"]]}, "zones": res.robot_zones}
    (pdir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return base, status


def test_matching_snapshot_passes(snapshot):
    base, status = snapshot
    assert status["zones"], "в срезе должны быть торгуемые зоны"
    lines: list[str] = []
    assert parity.run(base, out=lines.append) == 0
    assert "✔ Зоны и bias совпали" in "\n".join(lines)


def test_zone_shifted_by_3_pt_is_reported(snapshot, tmp_path):
    base, status = snapshot
    bad = json.loads(json.dumps(status))
    bad["zones"][0]["lo"] += 3.0
    bad["zones"][0]["hi"] += 3.0
    import shutil
    shutil.copytree(base / "parity", tmp_path / "parity")
    (tmp_path / "parity" / "status.json").write_text(json.dumps(bad), encoding="utf-8")
    lines: list[str] = []
    assert parity.run(tmp_path, out=lines.append) == 1
    assert "расхождение границ" in "\n".join(lines) or "не построил" in "\n".join(lines)


def test_tolerance_of_one_point_is_accepted(snapshot, tmp_path):
    base, status = snapshot
    near = json.loads(json.dumps(status))
    near["zones"][0]["lo"] += 0.9
    import shutil
    shutil.copytree(base / "parity", tmp_path / "parity")
    (tmp_path / "parity" / "status.json").write_text(json.dumps(near), encoding="utf-8")
    assert parity.run(tmp_path, out=lambda s: None) == 0


def test_bias_mismatch_and_missing_files(snapshot, tmp_path):
    base, status = snapshot
    import shutil
    shutil.copytree(base / "parity", tmp_path / "parity")
    other = json.loads(json.dumps(status))
    other["bias"]["d1"] = "SELL" if status["bias"]["d1"] != "SELL" else "BUY"
    (tmp_path / "parity" / "status.json").write_text(json.dumps(other), encoding="utf-8")
    lines: list[str] = []
    assert parity.run(tmp_path, out=lines.append) == 1 and "bias D1" in "\n".join(lines)
    empty: list[str] = []
    assert parity.run(tmp_path / "nothing", out=empty.append) == 2 and "MrCasinoExport" in empty[0]


def test_export_script_passes_the_linter():
    old = ROOT / "mt5" / "archive" / "MrCasino_v3.2.mq5"
    assert mql_lint.lint(ROOT / "mt5" / "MrCasinoExport.mq5", old) == 0
