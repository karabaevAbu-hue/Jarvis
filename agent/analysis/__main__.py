"""python -m agent.analysis zones [--fake] [--tf H4] [--side BUY] [--png out.png]"""
from __future__ import annotations

import argparse
import sys

from agent.analysis.chart import render_chart
from agent.analysis.engine import AnalysisEngine, render_text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.analysis")
    ap.add_argument("cmd", choices=["zones"])
    ap.add_argument("--fake", action="store_true", help="синтетические свечи вместо живого MT5")
    ap.add_argument("--csv", help="M5-свечи из CSV (time,open,high,low,close)")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--tf", default=None)
    ap.add_argument("--side", default=None, choices=["BUY", "SELL"])
    ap.add_argument("--png", default=None, help="сохранить график")
    a = ap.parse_args(argv)

    if a.fake or a.csv:
        from tools.fake_mt5 import CsvSource, FakeMT5Source
        src = CsvSource(a.csv) if a.csv else FakeMT5Source()
    else:
        from agent.analysis.data_mt5 import MT5Source
        from agent.config import get_settings
        src = MT5Source(get_settings().mt5_path)
    eng = AnalysisEngine(src, a.symbol)
    res = eng.analyze(tf=a.tf, side=a.side)
    print(render_text(res))
    if a.png:
        with open(a.png, "wb") as f:
            f.write(render_chart(eng.last_frames[a.tf or "H1"].df, res, tf=a.tf or "H1"))
        print(f"график: {a.png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
