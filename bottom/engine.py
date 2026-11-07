"""核心计算：等比后复权 + 历史最低价（含发行价）。

这里只有纯计算，不联网，方便测试。

口径说明
--------
* 复权：等比后复权。上市首日复权因子 = 1，之后每次除权除息，因子乘以
  “前一日收盘价 / 除权除息参考价”。后复权价 = 不复权价 × 因子。
  因为上市首日因子为 1，所以发行价可以直接和后复权价比较，不需要换算。
* 历史最低：上市以来每日最低价（或收盘价）的后复权值，再和发行价取最小。
* 比值：当前后复权价 / 历史最低。等比复权下，这个比值和用前复权价算出来的完全一样。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

# 价格精度是 0.01 元，差值达到半分钱就认为不同
PRICE_EPS = 0.005


def is_pos(x) -> bool:
    """是有限的正数。"""
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


@dataclass
class Bar:
    """一根不复权日线。"""

    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    preclose: float = math.nan  # 交易所公布的前收盘价（除权除息日为参考价）
    traded: bool = True  # False 表示停牌


@dataclass
class Stock:
    """一只股票的计算状态（每天增量更新，不需要保存全部历史）。"""

    code: str  # 6 位代码
    mkt: str  # sh / sz / bj
    name: str = ""
    list_date: str = ""  # 上市日期
    is_st: bool = False
    issue_price: float = math.nan  # 发行价
    issue_src: str = ""  # em = 东方财富新股数据；infer = 上市首日前收盘价推断
    ipo_preclose: float = math.nan  # 上市首日的前收盘价（交易所以发行价作为首日前收盘）
    adj: float = 1.0  # 当前后复权因子
    first_date: str = ""  # 数据里的第一根 K 线日期
    last_seen: str = ""  # 已处理到的日期（含停牌日）
    last_trade: str = ""  # 最后交易日
    last_close: float = math.nan  # 最后交易日的不复权收盘价
    last_adj: float = 1.0  # 最后交易日的复权因子（停牌期间除权时用来核对复牌日前收盘价）
    min_low: float = math.inf  # 历史最低价（日内最低，后复权）
    min_low_date: str = ""
    min_close: float = math.inf  # 历史最低收盘价（后复权）
    min_close_date: str = ""
    built: str = ""  # 最近一次用完整历史重算的日期
    n_events: int = 0  # 除权除息次数

    # ---------- 序列化 ----------
    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_record(self) -> dict:
        rec = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float) and not math.isfinite(v):
                v = ""
            rec[f.name] = v
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> "Stock":
        kwargs = {}
        for f in fields(cls):
            if f.name not in rec:
                continue
            v = rec[f.name]
            if f.type in ("float", float):
                if v in ("", None):
                    v = math.inf if f.name in ("min_low", "min_close") else math.nan
                else:
                    v = float(v)
            elif f.type in ("int", int):
                v = int(float(v)) if v not in ("", None) else 0
            elif f.type in ("bool", bool):
                v = str(v).strip().lower() in ("1", "true", "yes")
            else:
                v = "" if v is None else str(v)
                if f.name == "code":
                    v = v.zfill(6)
            kwargs[f.name] = v
        return cls(**kwargs)

    # ---------- 指标 ----------
    def close_hfq(self) -> float:
        """最后收盘价的后复权值。"""
        return self.last_close * self.last_adj

    def has_data(self) -> bool:
        return is_pos(self.last_close) and math.isfinite(self.min_low)

    def metrics(self, use_issue: bool = True) -> dict | None:
        """返回比值等指标；没有数据时返回 None。"""
        if not self.has_data():
            return None
        cur = self.close_hfq()
        base_low, base_close = self.min_low, self.min_close
        low_date, close_date = self.min_low_date, self.min_close_date
        if use_issue and is_pos(self.issue_price):
            if self.issue_price < base_low:
                base_low, low_date = self.issue_price, "发行价"
            if self.issue_price < base_close:
                base_close, close_date = self.issue_price, "发行价"
        return {
            # 当前价本身已计入历史最低，比值理论上不小于 1；这里消除浮点误差
            "ratio_low": max(1.0, cur / base_low),
            "ratio_close": max(1.0, cur / base_close),
            # 换算成“今天的价格口径”的历史最低价（相当于等比前复权），方便和现价对照
            "low_now": base_low / self.adj,
            "close_now": base_close / self.adj,
            "low_date": low_date,
            "close_date": close_date,
        }


def apply_bar(s: Stock, bar: Bar, adj: float | None = None) -> str:
    """把一根日线计入状态。

    adj：数据源给出的这一天的后复权因子（绝对值）。给了就直接用；
    没给就用“前收盘价 ≠ 上一日收盘价”来识别除权除息并自己算因子。

    返回值：skip / suspended / event / ok，用于统计。
    """
    if s.last_seen and bar.date <= s.last_seen:
        return "skip"  # 已经处理过，保证重复运行不会重复计算

    first = not s.first_date
    if first:
        s.first_date = bar.date
        if s.list_date and bar.date == s.list_date and is_pos(bar.preclose):
            s.ipo_preclose = bar.preclose

    event = False
    if adj is not None and is_pos(adj):
        if not first and not math.isclose(adj, s.adj, rel_tol=1e-9):
            event = True
        s.adj = adj
    elif not first and bar.traded and is_pos(s.last_close) and is_pos(bar.preclose):
        # 除权除息日：前收盘价是交易所算出的参考价，和“上一交易日收盘价”（换算到当前因子）不同。
        # 后复权价保持连续：参考价 × 新因子 = 上一交易日收盘价 × 旧因子
        expected = s.last_close * s.last_adj / s.adj
        if abs(bar.preclose - expected) >= PRICE_EPS:
            s.adj = s.last_close * s.last_adj / bar.preclose
            event = True
    if event:
        s.n_events += 1

    s.last_seen = bar.date
    if not bar.traded or not (is_pos(bar.low) and is_pos(bar.close)):
        return "event" if event else "suspended"

    hl = bar.low * s.adj
    hc = bar.close * s.adj
    if hl < s.min_low:
        s.min_low, s.min_low_date = hl, bar.date
    if hc < s.min_close:
        s.min_close, s.min_close_date = hc, bar.date
    s.last_close = bar.close
    s.last_adj = s.adj
    s.last_trade = bar.date
    return "event" if event else "ok"


def rebuild_from_history(
    prev: Stock, bars: list[Bar], adjs: list[float | None] | None, built: str
) -> Stock:
    """用完整历史从头重算一只股票。保留基本信息和已知的发行价。"""
    s = Stock(
        code=prev.code,
        mkt=prev.mkt,
        name=prev.name,
        list_date=prev.list_date,
        is_st=prev.is_st,
        issue_price=prev.issue_price,
        issue_src=prev.issue_src,
    )
    if adjs is None:
        adjs = [None] * len(bars)
    for bar, a in zip(bars, adjs):
        apply_bar(s, bar, a)
    s.built = built
    fill_issue_price(s)
    return s


def fill_issue_price(s: Stock) -> None:
    """没有正式发行价数据时，用上市首日前收盘价推断（交易所把发行价作为首日前收盘价）。"""
    if not is_pos(s.issue_price) and is_pos(s.ipo_preclose):
        s.issue_price = s.ipo_preclose
        s.issue_src = "infer"


def board_of(code: str, mkt: str) -> str:
    if mkt == "bj":
        return "北交所"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    return "沪主板" if mkt == "sh" else "深主板"


A_SHARE_PREFIX = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301", "302"),
}


def is_a_share(code: str, mkt: str) -> bool:
    if mkt == "bj":
        return True
    return code.startswith(A_SHARE_PREFIX.get(mkt, ()))
