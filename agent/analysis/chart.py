"""PNG-график с зонами, ликвидностью и текущей ценой (mplfinance)."""
from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from agent.analysis.models import AnalysisResult, Liquidity, Zone  # noqa: E402

GREEN, RED, GREY = "#1b9e5a", "#d6403a", "#6b7280"


def render_chart(df: pd.DataFrame, result: AnalysisResult | None = None, zones: list[Zone] | None = None,
                 liquidity: list[Liquidity] | None = None, price: float | None = None,
                 tf: str = "H1", bars: int = 120, title: str | None = None) -> bytes:
    """Свечи + прямоугольники зон + линии ликвидности + линия цены. Возвращает байты PNG."""
    zones = zones if zones is not None else (result.zones if result else [])
    liquidity = liquidity if liquidity is not None else (result.liquidity if result else [])
    price = price if price is not None else (result.price if result else float(df["close"].iloc[-1]))
    d = df.iloc[-bars:][["open", "high", "low", "close"]]
    lo_v, hi_v = float(d["low"].min()), float(d["high"].max())
    pad = (hi_v - lo_v) * 0.08
    lo_v, hi_v = lo_v - pad, hi_v + pad

    style = mpf.make_mpf_style(base_mpf_style="charles", gridstyle=":", gridcolor="#e5e7eb",
                               marketcolors=mpf.make_marketcolors(up=GREEN, down=RED, edge="inherit", wick="inherit"))
    fig, axes = mpf.plot(d, type="candle", style=style, returnfig=True, figsize=(12.8, 7.2),
                         xrotation=0, datetime_format="%d.%m %H:%M", warn_too_much_data=10_000)
    ax = axes[0]
    ax.set_ylim(lo_v, hi_v)
    x_right = len(d) - 1
    drawn = 0
    for z in zones:
        if z.hi < lo_v or z.lo > hi_v:
            continue
        color = GREEN if z.dir == "BUY" else RED
        ax.axhspan(z.lo, z.hi, color=color, alpha=0.22, lw=0)
        ax.axhline(z.lo, color=color, lw=0.6, alpha=0.6)
        ax.axhline(z.hi, color=color, lw=0.6, alpha=0.6)
        ax.text(0.5, z.hi, f" {z.dir} {z.stars} {z.status}", color=color, fontsize=8, va="bottom")
        drawn += 1
    # ликвидность: только major, не больше 8 ближайших к цене и без налезающих подписей
    shown: list[float] = []
    vis = sorted((q for q in liquidity if q.major and lo_v < q.price < hi_v), key=lambda q: abs(q.price - price))
    for q in vis[:8]:
        if any(abs(q.price - p) < 0.012 * (hi_v - lo_v) for p in shown):
            continue
        shown.append(q.price)
        ax.axhline(q.price, color=GREY, lw=0.7, ls="--", alpha=0.8)
        ax.text(x_right, q.price, f"{q.note} {q.price:.1f} ", color=GREY, fontsize=7, ha="right", va="bottom")
    ax.axhline(price, color="#111827", lw=0.9)
    ax.text(x_right, price, f" {price:.2f}", color="#111827", fontsize=8, ha="right", va="top")
    sym = result.symbol if result else ""
    ax.set_title(title or f"{sym} {tf} · зон на графике: {drawn}", fontsize=11, loc="left")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
