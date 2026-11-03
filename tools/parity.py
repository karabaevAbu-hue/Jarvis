"""Сверка зон движка с роботом на одном и том же срезе свечей (приёмка: расхождение <= 1 пункта).

1. На ПК с MT5 запустить скрипт mt5/MrCasinoExport.mq5 (на графике золота, при работающем роботе):
   он пишет Common\\Files\\MrCasino\\parity\\ (M5, M15, H1, H4, D1 + meta.json + status.json робота).
2. Затем:  python -m tools.parity --dir "%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files\\MrCasino"

Порт (agent/analysis/robot_port.py) пересчитывает зоны на выгруженных свечах, и их торгуемые зоны
сравниваются с zones из status.json. Код возврата 0 — совпало, 1 — есть расхождения, 2 — нет данных.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from agent.analysis.engine import AnalysisEngine, compare_with_robot
from agent.analysis.robot_port import RobotParams

TFS = ("M5", "M15", "H1", "H4", "D1")
BIAS = {"BUY": 1, "SELL": -1, "NONE": 0, "NEUTRAL": 0}


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
    return df[["open", "high", "low", "close"]].astype(float)


def run(base: Path, tol: float = 1.0, out=print, m15_ob: bool = False) -> int:
    pdir = base / "parity"
    try:
        meta = json.loads((pdir / "meta.json").read_text(encoding="utf-8"))
        raw = {tf: load_csv(pdir / f"{tf}.csv") for tf in TFS}
    except FileNotFoundError as e:
        out(f"✖ Нет выгрузки: {e.filename}. Запусти скрипт MrCasinoExport на графике золота.")
        return 2
    status_file = pdir / "status.json"
    if not status_file.exists():
        out("✖ Нет parity\\status.json: при выгрузке робот не был найден (проверь InpMagic в скрипте).")
        return 2
    status = json.loads(status_file.read_text(encoding="utf-8"))
    now = datetime.fromtimestamp(meta["now_utc"], tz=timezone.utc)
    tick = {"bid": meta["bid"], "ask": meta["ask"], "time": now}
    # InpUseM15OB: у v3.4 выключен по умолчанию; для робота v3.2 / v3.3 (или если включил сам) нужен флаг --m15-ob
    eng = AnalysisEngine(None, params=RobotParams(use_m15_ob=m15_ob))  # type: ignore[arg-type]
    res = eng.analyze_frames(raw, meta["symbol"], tick, now=now)
    robot_zones = status.get("zones", []) or []
    out(f"Символ {meta['symbol']} · bid {meta['bid']} · срез {now:%Y-%m-%d %H:%M:%S} UTC")
    out(f"Робот:  bias D1 {status.get('bias', {}).get('d1')} · H4 {status.get('bias', {}).get('h4')} · зон {len(robot_zones)}")
    out(f"Движок: bias D1 {res.bias['d1']} · H4 {res.bias['h4']} · зон {len(res.robot_zones)}")
    problems = compare_with_robot(res, robot_zones, tol)
    sb = status.get("bias", {})
    for key, mine in (("d1", res.bias["d1"]), ("h4", res.bias["h4"])):
        theirs = str(sb.get(key, "")).upper()
        if theirs and BIAS.get(theirs, 9) != BIAS.get(mine, 9):
            problems.append(f"bias {key.upper()}: робот {theirs}, движок {mine}")
    try:
        skew = abs((datetime.fromisoformat(status["ts_utc"].replace("Z", "+00:00")) - now).total_seconds())
        if skew > 60:
            out(f"⚠️ status.json снят на {skew:.0f} с позже/раньше среза: зоны могли обновиться, сверку лучше повторить")
    except (KeyError, ValueError):
        pass
    out("")
    for z in robot_zones:
        out(f"  робот   {z['dir']:<4} {z['lo']:>9.2f} – {z['hi']:>9.2f}  {z.get('status', ''):<9} балл {z.get('score', '')}")
    for z in res.robot_zones:
        out(f"  движок  {z['dir']:<4} {z['lo']:>9.2f} – {z['hi']:>9.2f}  {z['status']:<9} балл {z['score']}  {z['srcs']}")
    out("")
    if problems:
        out(f"✖ Расхождений: {len(problems)} (допуск {tol:g} pt)")
        for p in problems:
            out("  - " + p)
        return 1
    out(f"✔ Зоны и bias совпали с роботом (допуск {tol:g} pt).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dir", required=True, help=r"Common\Files\MrCasino (внутри папка parity)")
    ap.add_argument("--tol", type=float, default=1.0)
    ap.add_argument("--m15-ob", action="store_true", help="у робота InpUseM15OB=true (так было в v3.2 и v3.3)")
    a = ap.parse_args()
    sys.exit(run(Path(a.dir), a.tol, m15_ob=a.m15_ob))
