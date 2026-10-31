"""trades.csv моста и статистика /stats (раздел 13 ТЗ)."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

COLUMNS = ["date_open", "date_close", "ticket", "side", "type", "trigger", "mark", "fu_class", "score",
           "zone_sources", "zone_width", "session", "entry", "sl", "tp", "lot", "risk_usd", "result_pts",
           "result_usd", "rr_real", "exit", "strength", "weak", "manual", "duration_min"]

MIN_GROUP = 5  # группы меньше помечаются «мало данных»

_NUM = {"ticket": int, "score": float, "zone_width": float, "entry": float, "sl": float, "tp": float,
        "lot": float, "risk_usd": float, "result_pts": float, "result_usd": float, "rr_real": float,
        "strength": int, "duration_min": float}
_BOOL = ("weak", "manual")


def _to_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "да")


def read_trades(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rec: dict[str, Any] = dict(row)
            for k, cast in _NUM.items():
                try:
                    rec[k] = cast(rec[k]) if rec.get(k, "") != "" else 0
                except (ValueError, TypeError):
                    rec[k] = 0
            for k in _BOOL:
                rec[k] = _to_bool(rec.get(k, ""))
            out.append(rec)
    return out


def append_trade(path: Path, rec: dict[str, Any]) -> None:
    """Для симулятора: дозапись строки по колонкам (запятые в тексте заменяются пробелом)."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerow([str(rec.get(c, "")).replace(",", " ") for c in COLUMNS])


@dataclass
class Stat:
    n: int = 0
    wins: int = 0
    losses: int = 0
    be: int = 0
    pts: float = 0.0
    usd: float = 0.0
    rr_avg: float = 0.0
    profit_factor: float | None = None
    winrate: float | None = None

    @property
    def few(self) -> bool:
        return self.n < MIN_GROUP


def compute(trades: Iterable[dict[str, Any]]) -> Stat:
    """Винрейт без BE; profit factor = прибыль / убыток (None, если убытков нет)."""
    s = Stat()
    gross_win = gross_loss = 0.0
    rrs: list[float] = []
    for t in trades:
        s.n += 1
        usd = float(t.get("result_usd", 0))
        s.pts += float(t.get("result_pts", 0))
        s.usd += usd
        rrs.append(float(t.get("rr_real", 0)))
        if t.get("exit") == "BE":
            s.be += 1
        elif usd > 0:
            s.wins += 1
            gross_win += usd
        elif usd < 0:
            s.losses += 1
            gross_loss += -usd
        else:
            s.be += 1
    counted = s.wins + s.losses
    s.winrate = (s.wins / counted * 100.0) if counted else None
    s.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    s.rr_avg = sum(rrs) / len(rrs) if rrs else 0.0
    return s


def group_by(trades: list[dict[str, Any]], key: Callable[[dict], str]) -> dict[str, Stat]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        buckets[key(t)].append(t)
    return {k: compute(v) for k, v in sorted(buckets.items())}


STARS = {1: "★", 2: "★★", 3: "★★★"}

GROUPINGS: dict[str, Callable[[dict], str]] = {
    "триггер": lambda t: t.get("trigger") or "—",
    "тип": lambda t: t.get("type") or "—",
    "сессия": lambda t: t.get("session") or "—",
    "сила": lambda t: STARS.get(int(t.get("strength") or 0), "—"),
    "слабые/обычные": lambda t: "слабые" if t.get("weak") else "обычные",
    "сторона": lambda t: t.get("side") or "—",
    "выход": lambda t: t.get("exit") or "—",
}


def period_filter(trades: list[dict], date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Фильтр по дате закрытия (строки YYYY-MM-DD ...)."""
    out = []
    for t in trades:
        d = str(t.get("date_close", ""))[:10]
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        out.append(t)
    return out


def format_table(trades: list[dict], groupings: Iterable[str] | None = None) -> str:
    """Моноширинная таблица для сообщения (в блоке кода)."""
    if not trades:
        return "Закрытых сделок нет."
    total = compute(trades)
    lines = [_head("Итого"), _row("все", total)]
    for name in (groupings or GROUPINGS.keys()):
        fn = GROUPINGS[name]
        groups = group_by(trades, fn)
        if len(groups) <= 1:
            continue
        lines.append("")
        lines.append(_head(name))
        for k, st in groups.items():
            lines.append(_row(k, st))
    return "\n".join(lines)


def _head(title: str) -> str:
    return f"{title:<14}{'n':>3} {'win%':>5} {'pt':>8} {'usd':>9} {'rr':>5} {'pf':>5}"


def _row(label: str, st: Stat) -> str:
    wr = f"{st.winrate:.0f}" if st.winrate is not None else "—"
    pf = f"{st.profit_factor:.2f}" if st.profit_factor is not None else "—"
    tail = "  мало данных" if st.few else ""
    return f"{label:<14}{st.n:>3} {wr:>5} {st.pts:>+8.1f} {st.usd:>+9.2f} {st.rr_avg:>5.2f} {pf:>5}{tail}"
