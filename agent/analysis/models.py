"""Структуры движка анализа. Направление везде: BUY (зона/FU вверх) или SELL."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TF_ORDER = ["M5", "M15", "H1", "H4", "D1", "W1"]
TF_RANK = {tf: i for i, tf in enumerate(TF_ORDER)}


def opposite(d: str) -> str:
    return "SELL" if d == "BUY" else "BUY"


@dataclass
class FU:
    """Свеча, забравшая ликвидность и вернувшаяся внутри себя.

    dir=BUY: взяли ликвидность снизу и вернулись вверх. wick_lo..wick_hi — зона тени FU.
    """
    idx: int
    time: Any
    dir: str
    type: int            # 1..7 по таблице раздела 4 стратегии, 0 = не подошёл ни под один
    kind: str            # SFU | AFU
    cls: str             # A | B | C
    level: float         # забранный уровень (экстремум соседней свечи)
    wick_lo: float
    wick_hi: float
    tf: str = ""

    @property
    def height(self) -> float:
        return self.wick_hi - self.wick_lo


@dataclass
class Negation:
    idx: int
    time: Any
    dir: str             # куда разворот
    strong: bool
    count: int = 2       # 2 = Negation, 3 = тройная манипуляция x3
    tf: str = ""


@dataclass
class Retest:
    """Возврат в зону тени FU и отбой обычными свечами (FU re-test)."""
    fu: FU
    idx: int
    time: Any


@dataclass
class HCS:
    """FU, перетестированный другим FU. count: 1 = HCS, 2 = HCS x2, 3 = x3."""
    first: FU
    retest: FU
    count: int = 1
    in_htf_ob: bool = False
    tf: str = ""

    @property
    def dir(self) -> str:
        return self.first.dir


@dataclass
class OB:
    tf: str
    dir: str             # BUY = ищем покупки (Sell to Buy), SELL = ищем продажи (Buy to Sell)
    lo: float            # только тело odd-свечи
    hi: float
    idx: int
    time: Any
    has_fu_after: bool = False


@dataclass
class Liquidity:
    kind: str            # DOJI, EQ_HIGH, EQ_LOW, TREND, WICK, AFU, DIRTY_HIGH, DIRTY_LOW, FVG
    side: str            # HIGH — стопы над уровнем, LOW — под уровнем
    price: float
    major: bool
    tf: str
    note: str = ""
    touches: int = 0


@dataclass
class Imbalance:
    tf: str
    dir: str             # BUY = бычий FVG (разрыв вверх)
    lo: float
    hi: float
    idx: int
    time: Any
    filled: bool = False


@dataclass
class RetestLevel:
    """HTF Re-test Level (LAL Level) 1..4."""
    n: int
    price: float
    dir: str             # сценарий: SELL (цена подходит снизу вверх) или BUY (сверху вниз)
    tf: str
    label: str = ""


@dataclass
class Figure:
    """Фигура книги = карта стопов. liq_side/liq_price — где лежат стопы толпы."""
    kind: str
    tf: str
    retail_view: str     # что видит ретейл: BUY/SELL/WAIT
    liq_side: str
    liq_price: float
    text: str = ""


@dataclass
class Plan:
    entry_ref: float
    sl: float
    tp: float
    tp_lal: float | None
    risk_pts: float
    cancel: float
    stop_too_wide: bool = False
    text: str = ""


@dataclass
class Zone:
    dir: str
    lo: float
    hi: float
    status: str                      # NEW | CONFIRMED
    score: int
    type: str                        # WITH | MIXED | COUNTER
    srcs: list[str] = field(default_factory=list)
    htf: bool = False
    dist: float = 0.0
    hcs: int = 0
    notes: list[str] = field(default_factory=list)
    liquidity_under: Liquidity | None = None
    plan: Plan | None = None

    @property
    def width(self) -> float:
        return self.hi - self.lo

    @property
    def stars(self) -> str:
        return "★★★" if self.score >= 7 else "★★" if self.score >= 5 else "★"   # шкала Stars() робота

    def to_dict(self) -> dict:
        d = asdict(self)
        d["width"] = round(self.width, 2)
        d["stars"] = self.stars
        return d


@dataclass
class AnalysisResult:
    symbol: str
    price: float
    bias: dict[str, str]
    zones: list[Zone]
    liquidity: list[Liquidity] = field(default_factory=list)
    levels: list[RetestLevel] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    imbalances: list[Imbalance] = field(default_factory=list)
    candle_models: list[str] = field(default_factory=list)
    session: str = ""
    tf: str = "H1"
    frames_ok: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    robot_diff: list[str] = field(default_factory=list)
    robot_zones: list[dict] = field(default_factory=list)   # торгуемые зоны по правилам робота (для сверки)
