"""港股、美股：复权换算、发行价、列表解析、运行流程（全部用假数据，不联网）。"""

from __future__ import annotations

import datetime as dt
import json
import math
import os

import pytest

from bottom import store
from bottom.gengine import (GStock, YBar, choose_ipo, compute, cutoff_date, hist_cut, is_fund,
                            spac_like)
from bottom.gexport import export_market
from bottom.gpipeline import GlobalUpdater
from bottom.src_global import (clean_us_name, frame_to_bars, parse_hkex_frame, parse_nasdaq_files,
                               parse_nasdaq_ipo, parse_tencent)


def days(n, start="2024-01-02"):
    d0 = dt.date.fromisoformat(start)
    out, d = [], d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def yahoo_from_raw(dates, raw, events):
    """把不复权价格按 Yahoo 的方式调整：Close 按之后的拆股调整，Adj Close 再按之后的分红调整。

    raw: [(open, high, low, close)]；events: {下标: ("split", 倍数) 或 ("div", 每股现金)}"""
    n = len(raw)
    split_after = [1.0] * n
    div_after = [1.0] * n
    for i in range(n):
        for j, (kind, v) in events.items():
            if j > i and kind == "split":
                split_after[i] *= v
            if j > i and kind == "div":
                # 除息日前一天的收盘价（不复权）
                pc = raw[j - 1][3]
                div_after[i] *= 1 - v / pc
    bars = []
    for i, d in enumerate(dates):
        o, h, lo, c = (x / split_after[i] for x in raw[i])
        bars.append(YBar(d, o, h, lo, c, c * div_after[i]))
    splits = [(dates[j], v) for j, (k, v) in sorted(events.items()) if k == "split"]
    return bars, splits


def hfq_truth(raw, events, issue=None):
    """独立算法：按 A 股的等比后复权从头累乘因子，算 现价 / 历史最低。"""
    f, lows = 1.0, []
    for i, (o, h, lo, c) in enumerate(raw):
        if i in events:
            kind, v = events[i]
            pc = raw[i - 1][3]
            f *= v if kind == "split" else pc / (pc - v)
        lows.append(lo * f)
    base = min(lows)
    if issue is not None:
        base = min(base, issue)
    return raw[-1][3] * f / base


RAW = [(10.0, 10.5, 9.8, 10.2), (10.2, 10.4, 9.9, 10.0), (10.0, 10.1, 9.0, 9.2), (4.6, 4.8, 4.3, 4.5),
       (4.3, 4.4, 4.1, 4.2), (4.2, 4.3, 3.9, 4.1), (4.1, 4.6, 4.0, 4.5)]
EVENTS = {3: ("split", 2.0), 5: ("div", 0.15)}


def test_compute_matches_hfq_truth():
    dates = days(len(RAW))
    bars, splits = yahoo_from_raw(dates, RAW, EVENTS)
    s = GStock(code="00001", mkt="hk", issue_price=9.6, issue_date=dates[0])
    assert compute(s, bars, splits, [(dates[5], 0.15)], cutoff=dates[-1])
    assert s.ratio(use_issue=False) == pytest.approx(hfq_truth(RAW, EVENTS), rel=1e-9)
    assert s.ratio() == pytest.approx(hfq_truth(RAW, EVENTS, issue=9.6), rel=1e-9)
    assert s.last_close == pytest.approx(4.5) and s.n_splits == 1 and s.n_divs == 1
    # 历史最低换算成今天口径后，比现价低
    assert s.min_low < s.last_close


def test_cutoff_ignores_intraday_bar():
    dates = days(len(RAW))
    bars, splits = yahoo_from_raw(dates, RAW, EVENTS)
    s = GStock(code="X", mkt="us")
    compute(s, bars, splits, [], cutoff=dates[-2])
    assert s.last_trade == dates[-2] and s.last_close == pytest.approx(4.1)


def test_bad_low_repaired_and_jump_flagged():
    dates = days(5)
    bars = [YBar(d, 10, 10.5, 9.5, 10, 10) for d in dates]
    bars[2] = YBar(dates[2], 10, 10.5, 0.01, 10, 10)  # 错误成交
    s = GStock(code="X", mkt="us")
    compute(s, bars, [], [], dates[-1])
    assert s.n_fix == 1 and s.min_low == pytest.approx(9.5)
    bars[3] = YBar(dates[3], 50, 50, 50, 50, 50)  # 拆合股没有复权造成的跳变
    compute(s, bars, [], [], dates[-1])
    assert s.jump_date == dates[4]  # 50 → 10 也是跳变，记最近一次


def test_issue_checks():
    dates = days(5, "2020-03-02")
    bars = [YBar(d, 20, 21, 19, 20, 20) for d in dates]
    s = GStock(code="X", mkt="us", issue_price=18.0, issue_date="2020-02-28")  # 定价日在周五，周一上市
    compute(s, bars, [], [], dates[-1])
    assert s.issue_cmp == pytest.approx(18.0) and not s.issue_note
    s = GStock(code="X", mkt="us", issue_price=18.0, issue_date="2015-06-01")  # 代码被重复使用
    compute(s, bars, [], [], dates[-1])
    assert math.isnan(s.issue_cmp) and "对不上" in s.issue_note
    s = GStock(code="X", mkt="hk", issue_price=180.0, issue_date=dates[0])  # 数值不合理
    compute(s, bars, [], [], dates[-1])
    assert math.isnan(s.issue_cmp) and "相差过大" in s.issue_note
    # 上市后 1 拆 5：首日收盘 100（不复权）→ Yahoo 显示 20；招股价 80 换算后 16
    s = GStock(code="X", mkt="hk", issue_price=80.0, issue_date=dates[0])
    compute(s, bars, [("2024-05-15", 5.0)], [], dates[-1])
    assert s.issue_cmp == pytest.approx(16.0)


def test_choose_ipo_picks_matching_company():
    recs = [{"date": "2001-05-10", "price": 12.0}, {"date": "2021-09-29", "price": 18.0}]
    assert choose_ipo(recs, "2021-09-30")["price"] == 18.0
    assert choose_ipo(recs, "2010-01-04") is None


def test_hist_cut():
    assert hist_cut(GStock(code="00005", mkt="hk", list_date="1980-01-02", first_date="2000-01-03"))
    assert not hist_cut(GStock(code="00700", mkt="hk", list_date="2004-06-16", first_date="2004-06-16"))
    assert hist_cut(GStock(code="KO", mkt="us", first_date="1962-01-02"))
    assert not hist_cut(GStock(code="SNOW", mkt="us", first_date="2020-09-16"))


def test_spac_and_fund_detection():
    dates = days(120, "2025-01-02")
    flat = [YBar(d, 10.0, 10.02, 9.99, 10.0 + 0.001 * (i % 3), 10.0) for i, d in enumerate(dates)]
    assert spac_like("Foo Holdings Corp - Class A Ordinary Shares", flat, "2026-11-06")
    moving = [YBar(d, 10, 10, 10, 10 * (1.02 if i % 2 else 0.98), 10) for i, d in enumerate(dates)]
    assert not spac_like("Real Company Inc", moving, "2026-11-06")
    assert spac_like("Alpha Star Acquisition Corp", moving[:30], "2026-11-06")
    old = [YBar(d, 10, 10, 10, 10, 10) for d in days(120, "2015-01-02")]
    assert not spac_like("Sleepy Utility Co", old, "2026-11-06")  # 老股票不判为 SPAC
    assert is_fund("PIMCO Dynamic Income Fund")
    assert is_fund("Gabelli Equity Trust Inc. (The)")
    assert is_fund("Nuveen Municipal Value Fund, Inc.")
    for name in ("Federal Realty Investment Trust", "Permian Basin Royalty Trust", "Northern Trust Corporation",
                 "Apple Inc.", "Trustmark Corporation"):
        assert not is_fund(name), name


def test_parse_nasdaq_files():
    nasdaq = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
              "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
              "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
              "ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N\n"
              "ABCDW|ABCD Corp - Warrants|S|N|N|100|N|N\n"
              "BABA|Alibaba Group Holding Limited American Depositary Shares each representing eight Ordinary share|Q|N|N|100|N|N\n"
              "ARCC|Ares Capital Corporation - Closed End Fund|Q|N|N|100|N|N\n"
              "CHI|Calamos Convertible Opportunities and Income Fund - Closed End Fund|Q|N|N|100|N|N\n"
              "File Creation Time: 1109202608:00|||||||\n")
    other = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
             "BRK.B|Berkshire Hathaway Inc. Class B|N|BRKb|N|100|N|BRK.B\n"
             "ABR$D|Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred Stock|N|ABRpD|N|100|N|ABR-D\n"
             "PDI|PIMCO Dynamic Income Fund|N|PDI|N|100|N|PDI\n"
             "XYZ.U|XYZ Acquisition Corp Units|N|XYZ.U|N|100|N|XYZ=\n"
             "ET|Energy Transfer LP Common Units representing limited partner interests|N|ET|N|100|N|ET\n"
             "IEP|Icahn Enterprises L.P. - Depositary units|Q|IEP|N|100|N|IEP\n"
             "File Creation Time: 1109202608:00|||||||\n")
    got = parse_nasdaq_files([nasdaq, other])
    assert set(got) == {"AAPL", "BABA", "BRK.B", "PDI", "ET", "IEP", "ARCC", "CHI"}
    assert not got["ARCC"]["fund"] and got["CHI"]["fund"]  # 业务发展公司保留，封闭式基金排除
    assert got["ET"]["name_en"] == "Energy Transfer LP"
    assert got["AAPL"] == {"name_en": "Apple Inc.", "board": "纳斯达克", "fund": False}
    assert got["BRK.B"]["board"] == "纽交所" and got["PDI"]["fund"]
    assert clean_us_name("Alibaba Group Holding Limited American Depositary Shares each representing eight") == "Alibaba Group Holding Limited"


def test_parse_hkex_frame():
    pd = pytest.importorskip("pandas")
    raw = pd.DataFrame([
        ["List of Securities", None, None, None],
        ["Updated as at 09/11/2026", None, None, None],
        ["Stock Code", "Name of Securities", "Category", "Sub-Category"],
        [700, "TENCENT", "Equity", "Equity Securities (Main Board)"],
        [8001, "ORIENT SEC INT", "Equity", "Equity Securities (GEM)"],
        [80700, "TENCENT-R", "Equity", "Equity Securities (Main Board)"],
        [2800, "TRACKER FUND", "Exchange Traded Products", "Exchange Traded Funds"],
        [12345, "HSBC CALL", "Derivative Warrants", ""],
        [4335, "HSBC USD", "Equity", "Trading Only Securities"],
    ])
    got = parse_hkex_frame(raw)
    assert got == {"00700": {"name_en": "TENCENT", "board": "主板", "fund": False, "temp": False},
                   "08001": {"name_en": "ORIENT SEC INT", "board": "创业板", "fund": False, "temp": False}}
    raw2 = pd.DataFrame([
        ["Stock Code", "Name of Securities", "Category", "Sub-Category"],
        [2949, "GREEN POWER RTS", "Equity", "Equity Securities (Main Board)"],
        [8556, "S ROBOT EDU-OLD", "Equity", "Equity Securities (GEM)"],
        [1810, "XIAOMI-W", "Equity", "Equity Securities (Main Board)"],
        [1299, "AIA", "Equity", "Equity Securities (Main Board)"],
    ])
    got2 = parse_hkex_frame(raw2)
    assert [c for c, v in got2.items() if v["temp"]] == ["02949", "08556"]  # 供股权、合股期间的临时柜台


def test_parse_tencent_hk_us():
    text = ('v_hk00700="100~腾讯控股~00700~421.400~419.800~420.000~1000~' + "~" * 23 + '2026/11/09 16:08:09~' + "~" * 10 + '";\n'
            'v_usBRK.B="200~伯克希尔B~BRK.B.N~570.89~572.33~571~2000~' + "~" * 23 + '2026-11-06 16:03:41~' + "~" * 10 + '";')
    got = parse_tencent(text)
    assert got["hk00700"]["date"] == "2026-11-09" and got["hk00700"]["price"] == 421.4
    assert got["usBRK.B"]["name"] == "伯克希尔B" and got["usBRK.B"]["date"] == "2026-11-06"


def test_parse_nasdaq_ipo():
    j = {"data": {"priced": {"rows": [
        {"proposedTickerSymbol": "ACCV", "companyName": "Accelevation Holdings Corp.", "proposedSharePrice": "18.00",
         "pricedDate": "9/30/2026"},
        {"proposedTickerSymbol": "BAD", "proposedSharePrice": "", "pricedDate": "9/30/2026"}]}}}
    assert parse_nasdaq_ipo(j) == [{"sym": "ACCV", "price": 18.0, "date": "2026-09-30", "name": "Accelevation Holdings Corp."}]
    assert parse_nasdaq_ipo({"data": None}) == []


def test_frame_to_bars():
    pd = pytest.importorskip("pandas")
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-03", "2024-01-04"]).tz_localize("Asia/Hong_Kong")
    df = pd.DataFrame({"Open": [1, 2, 2.1, 3], "High": [1, 2, 2.1, 3], "Low": [1, 2, 2.1, 3], "Close": [1, 2, 2.1, 3],
                       "Adj Close": [0.9, 1.9, 2.0, 3], "Volume": [1, 1, 1, 1], "Dividends": [0, 0.1, 0.1, 0],
                       "Stock Splits": [0, 0, 0, 2.0]}, index=idx)
    bars, splits, n_div = frame_to_bars(df)
    assert [b.date for b in bars] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert bars[1].close == 2.1 and splits == [("2024-01-04", 2.0)] and n_div == [("2024-01-03", 0.1)]


def test_cutoff_date():
    utc = dt.timezone.utc
    assert cutoff_date("hk", dt.datetime(2026, 11, 9, 7, 0, tzinfo=utc)) == "2026-11-08"  # 港股 15:00 还没收盘
    assert cutoff_date("hk", dt.datetime(2026, 11, 9, 9, 10, tzinfo=utc)) == "2026-11-09"  # 港股 17:10
    assert cutoff_date("us", dt.datetime(2026, 11, 10, 22, 40, tzinfo=utc)) == "2026-11-10"  # 纽约 17:40
    assert cutoff_date("us", dt.datetime(2026, 11, 10, 18, 0, tzinfo=utc)) == "2026-11-09"  # 纽约 13:00 盘中


# ---------------- 运行流程 ----------------
class FakeGlobal:
    def __init__(self, mkt="hk", n=30, last_idx=40):
        self.mkt = mkt
        self.dates = days(60, "2024-03-01")
        self.last_idx = last_idx
        self.codes = [f"{i:05d}" for i in range(1, n + 1)] if mkt == "hk" else [f"S{i}" for i in range(n)]
        self.list_override = None
        self.fail = set()
        self.calls = []
        self.yahoo_lag = 0  # Yahoo 的完整历史比实际晚几天（港股收盘后几个小时内是 1）
        self.lag_except = set()  # 这些股票不受影响（比如刚上市的新股）
        self.no_trade = set()  # 当天停牌：腾讯行情没有最高、最低价

    def stock_list(self):
        codes = self.list_override if self.list_override is not None else self.codes
        return {c: {"name_en": f"CO {c}", "board": "主板", "fund": False} for c in codes}

    def raw(self, code):
        k = sum(map(ord, code)) % 7
        return [(10 + k + math.sin(i + k), 10.5 + k + math.sin(i + k), 9.5 + k + math.sin(i + k), 10 + k + math.sin(i + k))
                for i in range(self.last_idx + 1)]

    def histories(self, items, workers, deadline):
        self.calls.append(("histories", len(items)))
        for code, _sym in items:
            if code in self.fail:
                yield code, None, "没有数据"
                continue
            raw = self.raw(code)
            bars, splits = yahoo_from_raw(self.dates[: len(raw)], raw, {10: ("split", 2.0), 20: ("div", 0.3)})
            if self.yahoo_lag and code not in self.lag_except:
                bars = bars[: -self.yahoo_lag]
            yield code, (bars, splits, [(self.dates[20], 0.3)]), ""

    def latest_date(self):
        return self.dates[self.last_idx]

    def quotes(self, syms):
        out = {}
        for s in syms:
            raw = self.raw(s[2:])
            o, h, lo, c = raw[-1]
            if s[2:] in self.no_trade:
                h = lo = 0.0
            out[s] = {"name": "中文" + s[2:], "price": c, "preclose": raw[-2][3], "high": h, "low": lo,
                      "date": self.dates[self.last_idx]}
        return out

    def hk_issue_info(self):
        self.calls.append(("hk_issue_info", 0))
        return {c: {"price": 9.0, "list_date": self.dates[0], "name": "东财" + c} for c in self.codes}

    def us_ipo_month(self, ym):
        self.calls.append(("ipo", ym))
        return [{"sym": "S1", "price": 9.0, "date": "2024-02-29", "name": "S1 Corp"}] if ym == "2024-02" else []

    def infos(self, items, workers, deadline):
        self.calls.append(("infos", len(items)))
        for i, (code, _sym) in enumerate(items):
            if code in self.fail:
                yield code, None, "没有数据：x"
                continue
            k = i % 9
            yield code, {"currency": "HKD" if self.mkt == "hk" else "USD", "financialCurrency": "CNY",
                         "currentPrice": 10.0, "trailingEps": 0.5 + 0.1 * k, "trailingPE": 10.0 / (0.5 + 0.1 * k),
                         "priceToBook": 1 + 0.2 * k, "priceToSalesTrailing12Months": 2.0, "profitMargins": 0.1,
                         "returnOnEquity": 0.05 + 0.01 * k, "grossMargins": 0.3, "netIncomeToCommon": 100.0,
                         "operatingCashflow": 120.0, "revenueGrowth": 0.1 * k, "earningsGrowth": -0.05 * k,
                         "marketCap": 1e10, "sector": "Technology" if k % 2 else "Industrials",
                         "industry": f"Ind {k % 3}"}, ""


def run_g(src, d, idx_now, mode="daily"):
    stocks, meta = store.load(d, cls=GStock)
    src.last_idx = idx_now
    now = dt.datetime.fromisoformat(src.dates[idx_now] + "T12:00:00+00:00")  # 香港 20:00 / 纽约 08:00
    if src.mkt == "us":
        now += dt.timedelta(hours=12)  # 次日 00:00 UTC = 纽约 19:00/20:00
    up = GlobalUpdater(src.mkt, src, stocks, meta, state_dir=d, now=now, deadline=None, workers=2)
    up.run(mode)
    store.save(up.stocks, up.meta, d)
    return up


def test_global_run_end_to_end(tmp_path):
    d = str(tmp_path / "state")
    src = FakeGlobal("hk")
    up = run_g(src, d, 40)
    s = up.stocks["00001"]
    assert s.has_data() and s.name == "中文00001" and s.issue_src == "em"
    raw = src.raw("00001")
    assert s.ratio() == pytest.approx(hfq_truth(raw, {10: ("split", 2.0), 20: ("div", 0.3)}, issue=9.0), rel=1e-9)
    assert up.meta["market_date"] == src.dates[40] and up.meta["complete"]
    # 第二次运行没有新数据：不重新下载
    n = len(src.calls)
    up = run_g(src, d, 40)
    assert not any(c[0] == "histories" for c in src.calls[n:])
    assert any("不重新下载" in x for x in up.notes)
    # 新的一天：重新下载；失败的股票沿用上次结果
    src.fail = {"00002"}
    up = run_g(src, d, 41)
    s2 = up.stocks["00002"]  # Yahoo 下载失败：历史沿用上次的，最新一天用腾讯行情接上
    assert s2.fails == 1 and s2.fetched == src.dates[40] and s2.last_trade == s2.patched == src.dates[41]
    assert s2.last_close == pytest.approx(src.raw("00002")[-1][3])
    assert up.stocks["00001"].last_trade == src.dates[41]
    # 新股、退市
    src.codes = src.codes[1:] + ["09999"]
    up = run_g(src, d, 42)
    assert "00001" not in up.stocks and up.stocks["09999"].has_data()
    assert any("新上市" in x for x in up.notes) and any("退市" in x for x in up.notes)
    # 官方列表残缺：不删除
    src.list_override = src.codes[:5]
    up = run_g(src, d, 43)
    assert len(up.stocks) == len(src.codes)
    # 导出
    site = str(tmp_path / "site")
    info = export_market("hk", up.stocks, up.meta, site)
    doc = json.load(open(os.path.join(site, "data", "hk.json"), encoding="utf-8"))
    assert info["rows"] == len(doc["rows"]) == len(src.codes)
    r = dict(zip(doc["fields"], doc["rows"][0]))
    assert r["adj"] == 1 and r["hfq"] == r["close"] and r["issue_cmp"] is not None


def test_us_ipo_backfill_and_match(tmp_path, monkeypatch):
    import bottom.gpipeline as gp

    monkeypatch.setattr(gp, "IPO_BACKFILL_FROM", "2023-11")
    d = str(tmp_path)
    src = FakeGlobal("us", n=4)
    src.dates = days(60, "2024-03-01")
    up = run_g(src, d, 40)
    s1 = up.stocks["S1"]
    assert s1.issue_src == "nasdaq" and s1.issue_price == 9.0 and s1.list_date == src.dates[0]
    assert not up.stocks["S2"].issue_src
    months = [c[1] for c in src.calls if c[0] == "ipo"]
    assert "2023-11" in months and "2024-02" in months
    assert os.path.exists(os.path.join(d, "ipo_us.csv.gz"))
    # 已经回溯过的月份不再重复请求（最近 3 个月除外）
    n = len(src.calls)
    run_g(src, d, 41)
    months = [c[1] for c in src.calls[n:] if c[0] == "ipo"]
    assert "2023-11" not in months


def test_build_site_all_markets(tmp_path, monkeypatch):
    import shutil

    from bottom.__main__ import build_site

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shutil.copytree(os.path.join(root, "web"), tmp_path / "web")
    src = FakeGlobal("hk", n=10)
    run_g(src, str(tmp_path / "state-hk"), 40)
    monkeypatch.chdir(tmp_path)
    assert build_site() == 0
    m = json.load(open(tmp_path / "site" / "data" / "markets.json", encoding="utf-8"))
    assert m["hk"]["rows"] == 10 and m["us"]["rows"] == 0 and m["a"]["rows"] == 0
    for f in ("index.html", "app.js", "data/latest.json", "data/hk.json", "data/us.json", "data/hk.csv"):
        assert (tmp_path / "site" / f).exists(), f


def test_retry_redownloads_until_settled(tmp_path):
    d = str(tmp_path)
    src = FakeGlobal("hk", n=5)

    def run_at(hhmm):
        stocks, meta = store.load(d, cls=GStock)
        now = dt.datetime.fromisoformat(f"{src.dates[40]}T{hhmm}:00+08:00")
        up = GlobalUpdater("hk", src, stocks, meta, state_dir=d, now=now, workers=2)
        up.run("daily")
        store.save(up.stocks, up.meta, d)
        return sum(1 for c in src.calls if c[0] == "histories")

    assert run_at("17:10") == 1  # 收盘后 1 小时：下载，但还不算稳定
    assert run_at("17:40") == 2  # 再跑一次仍然下载
    assert run_at("21:10") == 3  # 晚上补跑：下载并标记为稳定
    assert run_at("21:40") == 3  # 之后没有新数据就跳过


# ---------------- 估值与财务、评分 ----------------
def _info(**kw):
    base = {"currency": "USD", "financialCurrency": "USD", "currentPrice": 100.0, "trailingEps": 5.0, "trailingPE": 20.0,
            "priceToBook": 3.0, "priceToSalesTrailing12Months": 2.0, "profitMargins": 0.1, "returnOnEquity": 0.15,
            "grossMargins": 0.4, "netIncomeToCommon": 150.0, "operatingCashflow": 200.0, "revenueGrowth": 0.08,
            "earningsGrowth": 0.12, "marketCap": 1e9, "sector": "Technology", "industry": "Software"}
    base.update(kw)
    return base


def test_apply_info_basic_and_checks():
    from bottom.gengine import apply_info, metric_value_g

    s = GStock(code="X", mkt="us", last_close=100.0)
    assert apply_info(s, _info(), "2026-11-09") == []
    assert (s.pe, s.pb, s.ps) == (20.0, 3.0, 2.0)
    assert s.roe == pytest.approx(15) and s.gpm == pytest.approx(40) and s.rev_yoy == pytest.approx(8)
    assert s.cfroe == pytest.approx(200 / (150 / 0.15) * 100)  # 经营现金流 / (净利润 / ROE)
    # 亏损：Yahoo 不给市盈率，用每股收益算出负的市盈率，排名时排最差
    s2 = GStock(code="Y", mkt="us")
    apply_info(s2, _info(trailingEps=-2.0, trailingPE=None, profitMargins=-0.1), "2026-11-09")
    assert s2.pe == pytest.approx(-50) and metric_value_g(s2, "ep") < 0
    # 负净资产（大额回购）：市净率、ROE 视为缺失，而不是最便宜
    s3 = GStock(code="Z", mkt="us")
    apply_info(s3, _info(priceToBook=-568.9, returnOnEquity=None), "2026-11-09")
    assert math.isnan(s3.pb) and math.isnan(s3.roe) and math.isnan(s3.cfroe)
    # ADR：报表币种人民币，Yahoo 市销率没换算（小了约 7 倍）→ 用 市盈率 × 净利率 推算
    s4 = GStock(code="BABA", mkt="us")
    flags = apply_info(s4, _info(financialCurrency="CNY", priceToSalesTrailing12Months=0.28), "2026-11-09")
    assert s4.ps == pytest.approx(2.0) and "市销率改用推算值" in flags
    # 亏损的 ADR：市销率无法核对，不用
    s5 = GStock(code="ADR", mkt="us")
    flags = apply_info(s5, _info(financialCurrency="CNY", trailingEps=-1, trailingPE=None, profitMargins=-0.2), "2026-11-09")
    assert math.isnan(s5.ps) and "币种不同、市销率无法核对" in flags
    # 交易币种不对（如美元柜台）：估值不用
    s6 = GStock(code="00001", mkt="hk")
    flags = apply_info(s6, _info(), "2026-11-09")
    assert "交易币种不符" in flags and math.isnan(s6.pe) and math.isnan(s6.mcap)
    # 银行没有毛利率（Yahoo 给 0）→ 缺失
    s7 = GStock(code="B", mkt="us")
    apply_info(s7, _info(grossMargins=0), "2026-11-09")
    assert math.isnan(s7.gpm)


def test_dividend_yield_from_history():
    dates = days(300, "2025-01-02")
    bars = [YBar(d, 10, 10, 10, 10, 10) for d in dates]
    s = GStock(code="X", mkt="hk")
    compute(s, bars, [], [("2025-01-10", 9.0), (dates[150], 0.2), (dates[250], 0.3)], dates[-1])
    assert s.dy == pytest.approx(5.0) and s.n_divs == 3  # 一年以前的那次不算
    compute(s, bars, [], [(dates[250], 3.0)], dates[-1])
    assert math.isnan(s.dy)  # 超过 25%（特别分红）不计


def test_g_scores_group_fallback():
    from bottom.gengine import g_scores

    stocks = []
    for i in range(30):
        st = GStock(code=f"{i:05d}", mkt="hk", last_close=10, min_low=5, fin_date="2026-11-09",
                    sector="Technology" if i < 20 else "Energy", industry="Software" if i < 10 else f"Misc{i}",
                    pe=5 + i, pb=1 + i / 10, dy=float(i % 5), roe=float(i), gpm=30.0, rev_yoy=float(i))
        stocks.append(st)
    stocks.append(GStock(code="99999", mkt="hk", last_close=10, min_low=5))  # 没有估值财务数据：不参与
    sc = g_scores(stocks)
    assert "99999" not in sc
    assert all(k in sc["00000"] for k in ("value", "quality", "growth", "total"))
    # 00000 在软件行业（10 只）里市盈率最低、ROE 最低
    assert sc["00000"]["value"] > sc["00009"]["value"]
    assert sc["00009"]["quality"] > sc["00000"]["quality"]


def test_fundamentals_in_pipeline(tmp_path):
    import bottom.gpipeline as gp

    d = str(tmp_path / "state")
    src = FakeGlobal("hk", n=20)
    up = run_g(src, d, 40)
    assert ("infos", 20) in src.calls
    s = up.stocks["00001"]
    assert s.fin_date and math.isfinite(s.pe) and s.sector
    assert any("估值与财务" in n and "20 / 20" in n for n in up.notes)
    # 一周内不重复取；超过一周再取
    n = len(src.calls)
    run_g(src, d, 41)
    assert not any(c[0] == "infos" for c in src.calls[n:])
    stocks, meta = store.load(d, cls=GStock)
    for st in stocks.values():
        st.fin_date = "2024-01-01"
    store.save(stocks, meta, d)
    run_g(src, d, 42)
    assert src.calls[-1] == ("infos", 20)
    # 导出带评分
    site = str(tmp_path / "site")
    stocks, meta = store.load(d, cls=GStock)
    export_market("hk", stocks, meta, site)
    doc = json.load(open(os.path.join(site, "data", "hk.json"), encoding="utf-8"))
    rows = [dict(zip(doc["fields"], r)) for r in doc["rows"]]
    assert all(r["tscore"] is not None for r in rows)
    assert rows[0]["sector"] in ("科技", "工业") and rows[0]["mcap"] > 0
    assert gp.FUND_MAX_MIN > 0


# ---------------- Yahoo 缺最新一天、腾讯补价 ----------------
def test_missing_adjclose_uses_previous_factor():
    dates = days(4)
    bars = [YBar(dates[0], 10, 10, 9, 10, 8.0), YBar(dates[1], 10, 10, 9.5, 10, 8.0),
            YBar(dates[2], 10, 10, 10, 10, 10.0), YBar(dates[3], 9, 9, 8.8, 9, math.nan)]  # 最新一天没有复权价
    s = GStock(code="X", mkt="hk")
    assert compute(s, bars, [], [], dates[-1])
    assert s.last_trade == dates[3] and s.last_close == 9.0
    assert s.min_low == pytest.approx(9 * 0.8)  # 第一天的最低价 × 分红调整系数；最新一天沿用系数 1


def test_patch_check_and_apply():
    from bottom.gengine import patch_apply, patch_check

    def st():
        return GStock(code="00001", mkt="hk", last_trade="2026-11-06", last_close=10.0, min_low=8.0,
                      min_low_date="2020-01-02", min_close=8.5, min_close_date="2020-01-02", dy=4.0)

    q = {"date": "2026-11-09", "price": 9.5, "preclose": 10.0, "high": 10.1, "low": 9.4}
    assert patch_check(st(), q, "2026-11-09") == "ok"
    assert patch_check(st(), q, "2026-11-08") == ""  # 这一天还没收盘，不用
    assert patch_check(st(), {**q, "date": "2026-11-06"}, "2026-11-09") == ""  # Yahoo 已经有这一天
    assert patch_check(st(), {**q, "high": 0.0, "low": 0.0}, "2026-11-09") == ""  # 停牌
    assert patch_check(st(), {**q, "preclose": 9.0}, "2026-11-09") == "mismatch"  # 中间缺了日子，或者拆股合股
    assert patch_check(st(), {**q, "low": 9.8}, "2026-11-09") == "mismatch"  # 最低价比现价还高：字段有问题
    s = st()
    patch_apply(s, q)
    assert (s.last_trade, s.last_close, s.patched) == ("2026-11-09", 9.5, "2026-11-09")
    assert s.min_low == 8.0 and s.ratio() == pytest.approx(9.5 / 8.0)
    assert s.dy == pytest.approx(4.0 * 10 / 9.5)
    s = st()
    patch_apply(s, {**q, "price": 7.9, "low": 7.5, "high": 9.9})  # 当天创新低
    assert (s.min_low, s.min_low_date) == (7.5, "2026-11-09") and (s.min_close, s.min_close_date) == (7.9, "2026-11-09")
    assert s.ratio() == pytest.approx(7.9 / 7.5)
    s = st()
    patch_apply(s, {**q, "low": 0.01})  # 明显错误的最低价不计
    assert s.min_low == 8.0


def test_common_date():
    from bottom.gengine import common_date

    def g(d, **kw):
        return GStock(code="x", mkt="hk", last_close=1.0, min_low=1.0, last_trade=d, **kw)

    assert common_date([g("2026-11-06")] * 5 + [g("2026-11-09")] * 2) == "2026-11-06"  # 少数新股领先不算
    assert common_date([g("2026-11-06")] * 2 + [g("2026-11-09")] * 2) == "2026-11-09"  # 并列取较晚的
    assert common_date([g("2026-11-09", excluded="spac")] * 9 + [g("2026-11-06")]) == "2026-11-06"
    assert common_date([]) == ""


def test_yahoo_lag_is_patched_from_tencent(tmp_path):
    """重现 2026-11-09 港股的情况：Yahoo 的完整历史没有当天的日线，只有刚上市的新股有。"""
    d = str(tmp_path)
    src = FakeGlobal("hk", n=30)
    run_g(src, d, 40)
    src.yahoo_lag, src.lag_except, src.no_trade = 1, {"00001", "00002"}, {"00030"}
    up = run_g(src, d, 41)
    day, prev = src.dates[41], src.dates[40]
    st = up.stocks
    assert up.meta["market_date"] == day
    assert st["00001"].last_trade == day and not st["00001"].patched  # Yahoo 自己有
    s = st["00010"]
    raw = src.raw("00010")
    assert s.last_trade == day and s.patched == day and s.last_close == pytest.approx(raw[-1][3])
    # 补上的结果和 Yahoo 有这一天时算出来的一样（当天没有除权除息）
    assert s.ratio() == pytest.approx(hfq_truth(raw, {10: ("split", 2.0), 20: ("div", 0.3)}, issue=9.0), rel=1e-9)
    assert st["00030"].last_trade == prev  # 当天停牌的不补
    assert sum(1 for x in st.values() if x.last_trade < up.meta["market_date"]) == 1  # 只有它显示“停牌”
    assert up.meta["settled_date"] == ""  # 靠补价的太多，不算稳定
    assert any("补上 27 只" in n for n in up.notes)
    # 下一次运行重新下载；Yahoo 有了这一天，补价标记清掉，数据稳定
    src.yahoo_lag = 0
    n = len(src.calls)
    up = run_g(src, d, 41)
    assert any(c[0] == "histories" for c in src.calls[n:])
    assert not any(x.patched for x in up.stocks.values())
    assert up.meta["settled_date"] == day and up.meta["market_date"] == day


def test_yahoo_lag_without_tencent_keeps_previous_date(tmp_path):
    d = str(tmp_path)
    src = FakeGlobal("hk", n=30)
    run_g(src, d, 40)
    src.yahoo_lag, src.lag_except = 1, {"00001", "00002"}
    src.quotes = lambda syms: {}  # 腾讯行情也取不到
    up = run_g(src, d, 41)
    # 多数股票只到前一天：数据日期按前一天，不会把大多数股票标成“停牌”
    assert up.meta["market_date"] == src.dates[40]
    assert up.meta["settled_date"] == ""
    assert any("下次运行重新下载" in n for n in up.notes)


def test_patch_skipped_when_more_than_one_day_behind(tmp_path):
    d = str(tmp_path)
    src = FakeGlobal("hk", n=60)
    run_g(src, d, 40)
    src.yahoo_lag = 2  # 差两天：腾讯的昨收和已有的最后收盘价接不上
    up = run_g(src, d, 43)
    assert not any(s.patched for s in up.stocks.values())
    assert any("这次不补" in n for n in up.notes)
    assert up.meta["market_date"] == src.dates[41]


def test_us_is_not_patched(tmp_path, monkeypatch):
    import bottom.gpipeline as gp

    monkeypatch.setattr(gp, "IPO_BACKFILL_FROM", "2024-01")
    d = str(tmp_path)
    src = FakeGlobal("us", n=20)
    run_g(src, d, 40)
    src.yahoo_lag = 1
    up = run_g(src, d, 41)
    assert not any(s.patched for s in up.stocks.values())
    assert up.meta["market_date"] == src.dates[40] and up.meta["settled_date"] == ""


def test_hk_temp_counters_excluded(tmp_path):
    d = str(tmp_path)
    src = FakeGlobal("hk", n=10)
    src.stock_list = lambda: {c: {"name_en": f"CO {c}-OLD" if c == "00003" else f"CO {c}", "board": "主板",
                                  "fund": False, "temp": c == "00003"} for c in src.codes}
    up = run_g(src, d, 40)
    assert up.stocks["00003"].excluded == "temp" and not up.stocks["00003"].has_data()
    site = str(tmp_path / "site")
    assert export_market("hk", up.stocks, up.meta, site)["rows"] == 9


def test_spac_rules():
    from bottom.gengine import shell_like

    few = [YBar(d, 10, 10, 10, 10.0, 10.0) for d in days(3, "2026-11-04")]
    assert spac_like("Haymaker Acquisition Corp V", few, "2026-11-09")  # 刚上市几天，名称就能判断
    assert not spac_like("Ives Ultra AI Opportunities Inc.", few, "2026-11-09")
    assert shell_like(GStock(code="IVAI", mkt="us", industry="Shell Companies", last_close=10.11))
    assert not shell_like(GStock(code="X", mkt="us", industry="Shell Companies", last_close=3.2))  # 并购后股价已经走开
    assert not shell_like(GStock(code="Y", mkt="us", industry="Software", last_close=10.0))


def test_intraday_run_then_evening_run(tmp_path):
    """2026-11-09 的实际时间线：中午（盘中）手动运行一次，晚上定时运行一次。Yahoo 正常时，晚上应当更新到当天。"""
    d = str(tmp_path)
    src = FakeGlobal("hk", n=12)
    src.dates = days(60, "2026-09-01")
    i = src.dates.index("2026-11-09")

    def run_at(iso_utc, lag=0):
        stocks, meta = store.load(d, cls=GStock)
        src.last_idx, src.yahoo_lag = i, lag
        up = GlobalUpdater("hk", src, stocks, meta, state_dir=d, now=dt.datetime.fromisoformat(iso_utc), workers=2)
        up.run("daily")
        store.save(up.stocks, up.meta, d)
        return up

    up = run_at("2026-11-09T04:00:00+00:00")  # 香港 12:00，盘中：当天这根不算，也不用腾讯补
    assert {s.last_trade for s in up.stocks.values()} == {"2026-11-06"} and up.meta["market_date"] == "2026-11-06"
    assert not any(s.patched for s in up.stocks.values())
    up = run_at("2026-11-09T14:00:00+00:00")  # 香港 22:00，Yahoo 已有当天
    assert {s.last_trade for s in up.stocks.values()} == {"2026-11-09"} and up.meta["settled_date"] == "2026-11-09"


def test_intraday_run_then_evening_run_with_yahoo_lag(tmp_path):
    """同样的时间线，但晚上 Yahoo 的完整历史还没有当天（实际发生的情况）：用腾讯行情补上，不再整片显示“停牌”。"""
    d = str(tmp_path)
    src = FakeGlobal("hk", n=12)
    src.dates = days(60, "2026-09-01")
    i = src.dates.index("2026-11-09")

    def run_at(iso_utc, lag):
        stocks, meta = store.load(d, cls=GStock)
        src.last_idx, src.yahoo_lag = i, lag
        up = GlobalUpdater("hk", src, stocks, meta, state_dir=d, now=dt.datetime.fromisoformat(iso_utc), workers=2)
        up.run("daily")
        store.save(up.stocks, up.meta, d)
        return up

    run_at("2026-11-09T04:00:00+00:00", 0)
    up = run_at("2026-11-09T14:00:00+00:00", 1)
    assert {s.last_trade for s in up.stocks.values()} == {"2026-11-09"}
    assert all(s.patched == "2026-11-09" for s in up.stocks.values())
    assert up.meta["market_date"] == "2026-11-09" and up.meta["settled_date"] == ""
