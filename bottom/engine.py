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
import re
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


class Record:
    """一行状态的读写（保存成 CSV）。字段类型由 dataclass 的注解决定。"""

    CODE_WIDTH = 0  # 代码不足这个长度时左边补 0（A 股 6 位）

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
                if f.name == "code" and cls.CODE_WIDTH:
                    v = v.zfill(cls.CODE_WIDTH)
            kwargs[f.name] = v
        return cls(**kwargs)


@dataclass
class Stock(Record):
    """一只股票的计算状态（每天增量更新，不需要保存全部历史）。"""

    CODE_WIDTH = 6

    code: str  # 6 位代码
    mkt: str  # sh / sz / bj
    name: str = ""
    list_date: str = ""  # 上市日期
    is_st: bool = False
    issue_price: float = math.nan  # 发行价
    issue_src: str = ""  # em = 东方财富新股数据；f10 = 东方财富发行资料；infer = 首日前收盘价推断；none = 换股/合并上市，没有发行价
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
    pe: float = math.nan  # 市盈率（TTM）
    pb: float = math.nan  # 市净率（MRQ）
    ps: float = math.nan  # 市销率（TTM）
    val_date: str = ""  # 估值数据日期
    industry: str = ""  # 所处行业（东方财富行业分类）
    roe: float = math.nan  # 净资产收益率 %（报告期累计）
    gpm: float = math.nan  # 销售毛利率 %
    ocfps: float = math.nan  # 每股经营现金流
    bps: float = math.nan  # 每股净资产
    rev_yoy: float = math.nan  # 营业总收入同比 %
    np_yoy: float = math.nan  # 净利润同比 %
    fin_period: str = ""  # 财务数据所属报告期

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


# 完整重算时保留的字段（与价格历史无关）
KEEP_ON_REBUILD = (
    "name", "list_date", "is_st", "issue_price", "issue_src",
    "pe", "pb", "ps", "val_date",
    "industry", "roe", "gpm", "ocfps", "bps", "rev_yoy", "np_yoy", "fin_period",
)


def rebuild_from_history(
    prev: Stock, bars: list[Bar], adjs: list[float | None] | None, built: str
) -> Stock:
    """用完整历史从头重算一只股票。只重算价格相关的字段；基本信息、发行价、估值、行业、财务数据原样保留。"""
    s = Stock(code=prev.code, mkt=prev.mkt, **{k: getattr(prev, k) for k in KEEP_ON_REBUILD})
    if adjs is None:
        adjs = [None] * len(bars)
    for bar, a in zip(bars, adjs):
        apply_bar(s, bar, a)
    s.built = built
    fill_issue_price(s)
    return s


def fill_issue_price(s: Stock) -> None:
    """没有正式发行价数据时，用上市首日前收盘价推断（交易所把发行价作为首日前收盘价）。

    换股吸收合并等方式上市的（issue_src = none）首日前收盘价是换股参考价，不是发行价，不推断。"""
    if s.issue_src == "none":
        return
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


# ---------------- 评分：价值 / 质量 / 成长 / 综合 ----------------
# 每个分数由几个指标组成，指标都换成“越大越好”，在同行业内排名换成百分位后取平均。
SCORE_DEFS = {
    # 价值：市盈率、市净率、市销率取倒数（盈利收益率、账面市值比、销售市值比）。
    # 取倒数后亏损、资不抵债会自然排到最差，而不是被当成“市盈率很低”。
    "value": (("ep", "bp", "sp"), 2),
    # 质量：净资产收益率、销售毛利率、经营现金流/净资产（利润有没有真金白银）
    "quality": (("roe", "gpm", "cfroe"), 2),
    # 成长：营业收入同比、净利润同比
    "growth": (("rev_yoy", "np_yoy"), 1),
}
TOTAL_WEIGHTS = {"value": 0.5, "quality": 0.3, "growth": 0.2}
MIN_GROUP = 8  # 同行业里有这个指标的股票少于它时，改为和全市场比较


def _fin(v) -> float:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else math.nan


def metric_value(s: "Stock", name: str) -> float:
    """指标值（越大越好）；缺失返回 nan。"""
    if name in ("ep", "bp", "sp"):
        v = _fin({"ep": s.pe, "bp": s.pb, "sp": s.ps}[name])
        return 1.0 / v if math.isfinite(v) and v != 0 else math.nan
    if name == "cfroe":
        return s.ocfps / s.bps if is_pos(s.bps) and math.isfinite(_fin(s.ocfps)) else math.nan
    return _fin(getattr(s, name))


def _percentiles(items: list[tuple[float, str]]) -> dict[str, float]:
    """[(值, 代码)] → {代码: 0～1 的百分位}，并列取平均名次。"""
    n = len(items)
    if n < 2:
        return {}
    items = sorted(items)
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        p = ((i + j) / 2) / (n - 1)
        for t in range(i, j + 1):
            out[items[t][1]] = p
        i = j + 1
    return out


def is_delisting(name: str) -> bool:
    """退市整理期：名称以“退”结尾（如“萃华退”），或旧规则下以“退市”开头。"""
    n = (name or "").strip()
    return bool(re.search(r"退$|^退市", n))


def compute_scores(stocks) -> dict[str, dict[str, float]]:
    """{代码: {value, quality, growth, total}}，各项 0–100，越高越好；缺的项不出现。

    同行业内比较，避免银行、地产这类天然低估值的行业永远“便宜”；
    行业太小或没有行业信息时，改为和全市场比较。
    退市整理期的股票不参与评分：股价已经崩塌，估值指标失去意义，也不占同行业的排名。"""
    stocks = [s for s in stocks if s.has_data() and not is_delisting(s.name)]
    pct: dict[str, dict[str, float]] = {}
    metrics = {m for ms, _ in SCORE_DEFS.values() for m in ms}
    for m in metrics:
        allv, groups = [], {}
        for s in stocks:
            v = metric_value(s, m)
            if math.isfinite(v):
                allv.append((v, s.code))
                if s.industry:
                    groups.setdefault(s.industry, []).append((v, s.code))
        market = _percentiles(allv)
        for code, p in market.items():
            pct.setdefault(code, {})[m] = p
        for items in groups.values():
            if len(items) >= MIN_GROUP:
                for code, p in _percentiles(items).items():
                    pct[code][m] = p
    out: dict[str, dict[str, float]] = {}
    for code, d in pct.items():
        sc = {}
        for name, (ms, need) in SCORE_DEFS.items():
            vals = [d[m] for m in ms if m in d]
            if len(vals) >= need:
                sc[name] = 100.0 * sum(vals) / len(vals)
        if all(k in sc for k in TOTAL_WEIGHTS):
            sc["total"] = sum(sc[k] * w for k, w in TOTAL_WEIGHTS.items())
        if sc:
            out[code] = sc
    return out


def value_scores(stocks) -> dict[str, float]:
    """只取价值分（0–100，越高越便宜）。"""
    return {c: d["value"] for c, d in compute_scores(stocks).items() if "value" in d}


def fin_period(today: str) -> str:
    """当前应该使用的财报期：所有公司都已过披露截止日的最近一期，保证全市场用同一期比较。

    一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30 截止；留 3 天给数据源整理。"""
    y, md = int(today[:4]), today[5:]
    if md >= "11-04":
        return f"{y}-09-30"
    if md >= "09-04":
        return f"{y}-06-30"
    if md >= "05-04":
        return f"{y}-03-31"
    return f"{y - 1}-09-30"
