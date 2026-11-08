"""模拟一个小型股票市场，用来测试复权与增量更新逻辑（不联网）。"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field

from bottom.engine import Bar
from bottom.src_other import Quote


@dataclass
class FakeStock:
    code: str
    mkt: str
    name: str
    list_idx: int  # 上市日在日历中的下标
    issue: float
    suspended: set = field(default_factory=set)  # 停牌日下标
    events: dict = field(default_factory=dict)  # 下标 -> ("div", 每股派现) 或 ("split", 每股送转后倍数)
    bars: dict = field(default_factory=dict)  # 下标 -> Bar（不复权）
    factor: dict = field(default_factory=dict)  # 下标 -> 真实后复权因子


def make_calendar(start="2024-01-01", n=160):
    d = dt.date.fromisoformat(start)
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    return days


class World:
    def __init__(self, seed=7, n_days=160):
        self.rng = random.Random(seed)
        self.cal = make_calendar(n=n_days)
        self.stocks: dict[str, FakeStock] = {}
        specs = [
            ("600001", "sh", "老股A", 0, 3.0),
            ("000002", "sz", "分红股B", 0, 8.0),
            ("300003", "sz", "送转股C", 5, 20.0),
            ("688004", "sh", "破发股D", 30, 50.0),
            ("002005", "sz", "*ST停牌E", 0, 6.0),
            ("601006", "sh", "新股F", 120, 12.0),
            ("920007", "bj", "北交G", 10, 9.0),
        ]
        for code, mkt, name, li, issue in specs:
            self.stocks[code] = FakeStock(code, mkt, name, li, issue)
        st = self.stocks
        st["000002"].events = {40: ("div", 0.5), 100: ("div", 0.8), 150: ("div", 0.3)}
        st["300003"].events = {60: ("split", 2.0), 130: ("div", 0.2)}
        st["002005"].suspended = set(range(70, 90))
        st["002005"].events = {80: ("div", 0.4)}  # 停牌期间除权，复牌日前收盘价为参考价
        st["920007"].events = {90: ("div", 0.3), 140: ("split", 1.5)}
        st["601006"].events = {}
        for s in st.values():
            self._simulate(s)

    def _simulate(self, s: FakeStock):
        rng = self.rng
        close = s.issue
        f = 1.0
        first = True
        for i in range(s.list_idx, len(self.cal)):
            d = self.cal[i]
            if first:
                pre = s.issue  # 上市首日前收盘价 = 发行价
                chg = 0.3 if s.code != "688004" else -0.1  # D 首日破发
            else:
                pre = close
                ev = s.events.get(i)
                if ev:
                    kind, v = ev
                    ref = round(close - v, 2) if kind == "div" else round(close / v, 2)
                    f *= close / ref
                    pre = ref
                chg = rng.uniform(-0.06, 0.06)
                if s.code == "688004":
                    chg -= 0.004  # 一路下跌
            if i in s.suspended:
                s.bars[i] = Bar(d, pre, pre, pre, pre, pre, traded=False)
                s.factor[i] = f
                close = pre
                continue
            c = round(max(0.5, pre * (1 + chg)), 2)
            lo = round(min(pre, c) * (1 - rng.uniform(0, 0.03)), 2)
            hi = round(max(pre, c) * (1 + rng.uniform(0, 0.03)), 2)
            o = round(pre * (1 + rng.uniform(-0.01, 0.01)), 2)
            o = min(max(o, lo), hi)
            s.bars[i] = Bar(d, o, hi, lo, c, pre, traded=True)
            s.factor[i] = f
            close = c
            first = False

    # ---------- 真实答案 ----------
    def truth(self, code, upto_idx, use_issue=True):
        s = self.stocks[code]
        idx = [i for i in sorted(s.bars) if i <= upto_idx]
        traded = [i for i in idx if s.bars[i].traded]
        if not traded:
            return None
        lows = [s.bars[i].low * s.factor[i] for i in traded]
        closes = [s.bars[i].close * s.factor[i] for i in traded]
        base_l, base_c = min(lows), min(closes)
        if use_issue:
            base_l, base_c = min(base_l, s.issue), min(base_c, s.issue)
        last = traded[-1]
        cur = s.bars[last].close * s.factor[last]
        return cur / base_l, cur / base_c


class FakeSources:
    """模拟 BaoStock / 腾讯 / 东方财富。"""

    def __init__(self, world: World, today_idx: int, bs_lag: int = 0, drop: set | None = None,
                 em_prices: bool = True):
        self.w = world
        self.today_idx = today_idx
        self.bs_lag = bs_lag  # BaoStock 入库延迟（天）
        self.drop = drop or set()  # (代码, 下标)：模拟全市场日线里缺了某只股票
        self.em_prices = em_prices
        self.calls = []

    @property
    def today(self):
        return self.w.cal[self.today_idx]

    def _avail_idx(self):
        return self.today_idx - self.bs_lag

    # BaoStock
    def stock_basic(self):
        return [
            {"code": f"{s.mkt}.{s.code}", "code_name": s.name, "ipoDate": self.w.cal[s.list_idx],
             "outDate": "", "type": "1", "status": "1"}
            for s in self.w.stocks.values() if s.mkt != "bj" and s.list_idx <= self.today_idx + 5
        ]

    def trade_dates(self, start, end):
        return [d for d in self.w.cal if start <= d <= end]

    def latest_date(self):
        return self.w.cal[self._avail_idx()]

    def daily_bars(self, date):
        self.calls.append(("daily_bars", date))
        i = self.w.cal.index(date)
        if i > self._avail_idx():
            return {}
        out = {}
        for s in self.w.stocks.values():
            if s.mkt == "bj" or i not in s.bars or (s.code, i) in self.drop:
                continue
            out[s.code] = (s.mkt, s.bars[i], False, self.valuation(s, i))
        return out

    # 每股收益、每股净资产、每股销售额（模拟）；688004 亏损
    FUND = {"600001": (0.3, 2.0, 1.5), "000002": (0.8, 6.0, 9.0), "300003": (1.0, 5.0, 4.0),
            "688004": (-2.0, 30.0, 10.0), "002005": (-0.1, 1.0, 3.0), "601006": (0.5, 4.0, 6.0),
            "920007": (0.6, 5.0, 7.0)}

    INDUSTRY = {"600001": "银行", "000002": "银行", "300003": "电子", "688004": "电子",
                "002005": "化工", "601006": "化工", "920007": "电子"}

    def financials(self, period):
        self.calls.append(("financials", period))
        out = []
        for s in self.w.stocks.values():
            if s.list_idx > self.today_idx:
                continue
            e, bv, sp = self.FUND[s.code]
            out.append({"code": s.code, "industry": self.INDUSTRY[s.code], "bps": bv, "roe": e / bv * 100,
                        "gpm": math.nan if self.INDUSTRY[s.code] == "银行" else 25.0 + bv,
                        "ocfps": e * 1.1, "rev_yoy": 10.0 * e, "np_yoy": 20.0 * e})
        return out

    def valuation(self, s, i):
        b = s.bars[i]
        if not b.traded:
            return (math.nan, math.nan, math.nan)
        e, bv, sp = self.FUND[s.code]
        return (b.close / e, b.close / bv, b.close / sp)

    def daily_factors(self, date):
        i = self.w.cal.index(date)
        return {s.code: s.factor[i] for s in self.w.stocks.values()
                if s.mkt != "bj" and i in s.factor and i in s.events}

    def history_shsz(self, code, mkt, end):
        self.calls.append(("history", code))
        s = self.w.stocks[code]
        e = self.w.cal.index(end)
        idx = [i for i in sorted(s.bars) if i <= e]
        return [s.bars[i] for i in idx], [s.factor[i] for i in idx]

    def factors_shsz(self, code, mkt, end):
        self.calls.append(("factors", code))
        s = self.w.stocks[code]
        e = self.w.cal.index(end)
        return [(self.w.cal[i], s.factor[i]) for i in sorted(s.events) if i <= e and i in s.factor]

    def fetch_shsz(self, items, end, kind="history"):
        for code, mkt in items:
            if kind == "factors":
                yield code, self.factors_shsz(code, mkt, end), None, ""
            else:
                bars, adjs = self.history_shsz(code, mkt, end)
                yield code, bars, adjs, ""

    # 北交所 / 腾讯 / 东方财富
    def bj_stock_list(self):
        return [{"code": s.code, "name": s.name, "list_date": self.w.cal[s.list_idx]}
                for s in self.w.stocks.values() if s.mkt == "bj" and s.list_idx <= self.today_idx]

    def bj_history(self, code, list_date):
        self.calls.append(("bj_history", code))
        s = self.w.stocks[code]
        idx = [i for i in sorted(s.bars) if i <= self.today_idx and s.bars[i].traded]
        bars = [Bar(b.date, b.open, b.high, b.low, b.close, math.nan, True) for b in (s.bars[i] for i in idx)]
        f0 = s.factor[idx[0]]
        return bars, [s.factor[i] / f0 for i in idx]

    def quotes(self, syms):
        out = {}
        for sym in syms:
            s = self.w.stocks.get(sym[2:])
            b = s.bars.get(self.today_idx) if s else None
            if b is None:
                continue
            out[sym] = Quote(sym, s.name, b.close, b.preclose, b.open, b.high, b.low,
                             1000 if b.traded else 0, b.date, "15:00:00")
        return out

    def issue_prices(self):
        if not self.em_prices:
            return {}
        # 只有 2 只有东方财富数据，其余靠首日前收盘价推断
        return {"688004": 50.0, "601006": 12.0, "920007": 9.0}

    def close(self):
        pass
