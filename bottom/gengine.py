"""港股、美股的核心计算（纯计算，不联网，只用标准库，方便测试）。

和 A 股不同，港美股每天直接用 Yahoo 的完整历史从头重算，不做增量：
下载完整历史和只下载最近几天的请求次数一样多，从头重算能自动反映新的分红、拆合股和数据源的事后修正。

Yahoo 的价格口径
----------------
* Open / High / Low / Close 已按拆股、合股调整（以今天的股本为准）；
* Adj Close 在此基础上再按分红等比调整，最后一天 Adj Close = Close。
所以 Low × (Adj Close ÷ Close) 就是“今天口径”的等比复权最低价，可以直接和现价比较。
比值 = 现价 ÷ 历史最低，和 A 股用后复权算出的结果口径一致（等比复权下前后复权的比值相同）。

发行价换算到今天口径：发行价 ÷ 上市后累计拆股倍数 × 上市首日的分红调整系数。
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass

from .engine import Record, is_pos


@dataclass
class YBar:
    """Yahoo 的一根日线。"""

    date: str
    open: float
    high: float
    low: float
    close: float  # 已按拆合股调整
    adjclose: float  # 再按分红调整


@dataclass
class GStock(Record):
    """一只港股或美股的状态（每天用完整历史重算后保存结果）。价格都是“今天口径”。"""

    code: str  # 港股 5 位代码（00700）；美股代码（AAPL、BRK.B）
    mkt: str  # hk / us
    name: str = ""  # 中文名（腾讯行情），没有时用英文名
    name_en: str = ""  # 官方列表里的英文名
    board: str = ""  # 港股：主板 / 创业板；美股：交易所
    added: str = ""  # 第一次出现在官方列表的日期
    list_date: str = ""  # 上市日期（港股来自东方财富；美股来自 IPO 日历）
    issue_price: float = math.nan  # 发行价（原始价格）
    issue_src: str = ""  # em = 东方财富港股资料；nasdaq = 纳斯达克 IPO 日历
    issue_date: str = ""  # 来源里的上市日 / 定价日
    issue_cmp: float = math.nan  # 发行价换算到今天口径（可以直接和历史最低比较）
    issue_note: str = ""  # 有发行价但没有采用的原因
    first_date: str = ""  # 数据里的第一根 K 线
    last_trade: str = ""
    last_close: float = math.nan  # 最后交易日收盘价
    min_low: float = math.inf  # 历史最低价（日内最低，今天口径）
    min_low_date: str = ""
    min_close: float = math.inf  # 历史最低收盘价（今天口径）
    min_close_date: str = ""
    n_splits: int = 0
    n_divs: int = 0
    n_fix: int = 0  # 修正过的异常最低价根数
    jump_date: str = ""  # 最近一次单日涨跌超过 4 倍的日期（可能是拆合股没有复权）
    excluded: str = ""  # spac / fund：不显示
    fetched: str = ""  # 最近一次成功下载的日期
    fails: int = 0  # 连续下载失败次数

    def has_data(self) -> bool:
        return is_pos(self.last_close) and math.isfinite(self.min_low)

    def ratio(self, use_issue: bool = True) -> float:
        base = self.min_low
        if use_issue and is_pos(self.issue_cmp):
            base = min(base, self.issue_cmp)
        return max(1.0, self.last_close / base)


JUMP = 4.0  # 单日涨跌超过 4 倍视为可疑跳变
LOW_FIX = 0.5  # 最低价比开盘、收盘中较低者还低一半以上，视为错误成交，改用开盘/收盘中较低者
ISSUE_RANGE = {"hk": (0.2, 3.0), "us": (0.1, 3.0)}  # 发行价 ÷ 上市首日收盘价 的合理范围
ISSUE_DAYS = (-3, 10)  # 数据第一天 − 上市日（或定价日）的允许天数


def _days(a: str, b: str) -> int:
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def compute(s: GStock, bars: list[YBar], splits: list[tuple[str, float]], n_divs: int, cutoff: str) -> bool:
    """用完整历史重算一只股票。cutoff 之后的 K 线（盘中未收盘）不计入。没有有效数据返回 False，状态不变。"""
    bars = [b for b in bars if b.date <= cutoff and is_pos(b.close) and is_pos(b.adjclose)]
    if not bars:
        return False
    min_low, min_low_date, min_close, min_close_date = math.inf, "", math.inf, ""
    n_fix, jump = 0, ""
    prev = None
    for b in bars:
        f = b.adjclose / b.close
        ref = min(b.close, b.open) if is_pos(b.open) else b.close
        lo = b.low if is_pos(b.low) else ref
        if lo < LOW_FIX * ref:
            lo, n_fix = ref, n_fix + 1
        lo = min(lo, ref)  # 最低价不会高于开盘、收盘价（数据错误时）
        if lo * f < min_low:
            min_low, min_low_date = lo * f, b.date
        if b.adjclose < min_close:
            min_close, min_close_date = b.adjclose, b.date
        if prev is not None:
            r = b.close / prev.close
            if r > JUMP or r < 1 / JUMP:
                jump = b.date
        prev = b
    last = bars[-1]
    k = last.close / last.adjclose  # 正常为 1；统一到“最后收盘价”口径
    s.first_date, s.last_trade, s.last_close = bars[0].date, last.date, last.close
    s.min_low, s.min_low_date = min_low * k, min_low_date
    s.min_close, s.min_close_date = min_close * k, min_close_date
    s.n_splits = sum(1 for d, r in splits if d <= cutoff and is_pos(r) and r != 1)
    s.n_divs, s.n_fix, s.jump_date = n_divs, n_fix, jump
    s.issue_cmp, s.issue_note = math.nan, ""
    if is_pos(s.issue_price):
        convert_issue(s, bars[0], splits, k)
    return True


def convert_issue(s: GStock, first: YBar, splits: list[tuple[str, float]], k: float = 1.0) -> None:
    """把发行价换算到今天口径，并检查它确实属于这段历史（避免代码被重复使用、数据缺了开头几年）。"""
    if s.issue_date:
        gap = _days(first.date, s.issue_date)
        if not ISSUE_DAYS[0] <= gap <= ISSUE_DAYS[1]:
            s.issue_note = f"数据从 {first.date} 开始，和上市日 {s.issue_date} 对不上"
            return
    cum = 1.0
    for d, r in splits:
        if d > first.date and is_pos(r):
            cum *= r
    raw_first_close = first.close * cum
    lo, hi = ISSUE_RANGE.get(s.mkt, (0.1, 3.0))
    if not lo <= s.issue_price / raw_first_close <= hi:
        s.issue_note = f"发行价 {s.issue_price:g} 和上市首日收盘价 {raw_first_close:.4g} 相差过大"
        return
    s.issue_cmp = s.issue_price / cum * (first.adjclose / first.close) * k


def choose_ipo(records: list[dict], first_date: str) -> dict | None:
    """同一代码可能先后属于不同公司：选定价日和数据第一天最接近、且在允许范围内的那条 IPO 记录。"""
    best, best_gap = None, None
    for r in records:
        if not r.get("date") or not is_pos(r.get("price")):
            continue
        gap = _days(first_date, r["date"])
        if ISSUE_DAYS[0] <= gap <= ISSUE_DAYS[1] and (best_gap is None or abs(gap) < best_gap):
            best, best_gap = r, abs(gap)
    return best


# ---------------- 美股：SPAC 和封闭式基金 ----------------
SPAC_NAME = re.compile(r"\b(acquisition|merger)\b|blank check|\bspac\b", re.I)
FUND_NAME = re.compile(r"\bfunds?\b|closed[- ]end|\bmunicipal\b|\bmuni\b|total return", re.I)
TRUST_FUND = re.compile(
    r"\b(income|bond|credit|dividend|equity|opportunit\w*|infrastructure|strategic|premium|tax[- ]advantaged|"
    r"utilit\w*|science and technology)\b.*\btrust\b", re.I)
NOT_FUND = re.compile(r"realty|real estate|\breit\b|royalty|propert|mortgage|residential|apartment|hotel|lodging|"
                      r"storage|net lease|bancorp|\bbank", re.I)


def is_fund(name: str) -> bool:
    """封闭式基金（按名称判断）：名称带 Fund，或“收益/债券/股息……Trust”这类（房地产、矿权信托除外）。"""
    n = name or ""
    if FUND_NAME.search(n):
        return True
    return bool(TRUST_FUND.search(n)) and not NOT_FUND.search(n)


def spac_like(name: str, bars: list[YBar], today: str) -> bool:
    """并购前的 SPAC：近 4 年上市，股价一直贴着信托价值（约 10 美元）几乎不动。

    完成并购后股价开始正常波动，就不再满足条件，会自动显示出来。"""
    closes = [b.close for b in bars if is_pos(b.close)]
    if not closes or _days(today, bars[0].date) > 4 * 366:
        return False
    if len(closes) >= 60 and all(9.0 <= c <= 12.5 for c in closes):
        rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        if statistics.median(rets) <= 0.004:
            return True
    return bool(SPAC_NAME.search(name or "")) and len(closes) >= 10 and all(8.0 <= c <= 15.0 for c in closes)


# ---------------- 交易时间 ----------------
MARKET_TZ = {"hk": "Asia/Hong_Kong", "us": "America/New_York"}
DAY_FINAL = {"hk": "16:30", "us": "16:45"}  # 当地时间过了这个点，当天的日线才算收盘定稿


def cutoff_date(mkt: str, now_utc: dt.datetime) -> str:
    """只使用已经收盘的日线：收盘定稿前运行时，不计入今天这根（盘中价格）。"""
    from zoneinfo import ZoneInfo

    local = now_utc.astimezone(ZoneInfo(MARKET_TZ[mkt]))
    d = local.date()
    if local.strftime("%H:%M") < DAY_FINAL[mkt]:
        d -= dt.timedelta(days=1)
    return d.isoformat()


DAY_SETTLED = {"hk": "19:00", "us": "19:30"}  # 收盘后过了这个点，认为 Yahoo 的当日数据已经稳定（不会再修正）


def settled(mkt: str, now_utc: dt.datetime, cutoff: str) -> bool:
    """这次用到的最后一天数据是否已经稳定：运行时间在当地 DAY_SETTLED 之后，或已经是第二天。"""
    from zoneinfo import ZoneInfo

    local = now_utc.astimezone(ZoneInfo(MARKET_TZ[mkt]))
    return local.date().isoformat() > cutoff or local.strftime("%H:%M") >= DAY_SETTLED[mkt]


HIST_START = {"hk": "2000-01-31", "us": "1962-01-31"}  # Yahoo 历史数据的大致起点


def hist_cut(s: GStock) -> bool:
    """历史数据可能没有覆盖到上市日：上市日早于数据起点，或数据正好从 Yahoo 的最早日期开始。"""
    if not s.first_date:
        return False
    if s.list_date:
        return _days(s.first_date, s.list_date) > ISSUE_DAYS[1]
    return s.first_date <= HIST_START.get(s.mkt, "")
