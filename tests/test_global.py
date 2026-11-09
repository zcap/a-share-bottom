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
    assert compute(s, bars, splits, 1, cutoff=dates[-1])
    assert s.ratio(use_issue=False) == pytest.approx(hfq_truth(RAW, EVENTS), rel=1e-9)
    assert s.ratio() == pytest.approx(hfq_truth(RAW, EVENTS, issue=9.6), rel=1e-9)
    assert s.last_close == pytest.approx(4.5) and s.n_splits == 1 and s.n_divs == 1
    # 历史最低换算成今天口径后，比现价低
    assert s.min_low < s.last_close


def test_cutoff_ignores_intraday_bar():
    dates = days(len(RAW))
    bars, splits = yahoo_from_raw(dates, RAW, EVENTS)
    s = GStock(code="X", mkt="us")
    compute(s, bars, splits, 1, cutoff=dates[-2])
    assert s.last_trade == dates[-2] and s.last_close == pytest.approx(4.1)


def test_bad_low_repaired_and_jump_flagged():
    dates = days(5)
    bars = [YBar(d, 10, 10.5, 9.5, 10, 10) for d in dates]
    bars[2] = YBar(dates[2], 10, 10.5, 0.01, 10, 10)  # 错误成交
    s = GStock(code="X", mkt="us")
    compute(s, bars, [], 0, dates[-1])
    assert s.n_fix == 1 and s.min_low == pytest.approx(9.5)
    bars[3] = YBar(dates[3], 50, 50, 50, 50, 50)  # 拆合股没有复权造成的跳变
    compute(s, bars, [], 0, dates[-1])
    assert s.jump_date == dates[4]  # 50 → 10 也是跳变，记最近一次


def test_issue_checks():
    dates = days(5, "2020-03-02")
    bars = [YBar(d, 20, 21, 19, 20, 20) for d in dates]
    s = GStock(code="X", mkt="us", issue_price=18.0, issue_date="2020-02-28")  # 定价日在周五，周一上市
    compute(s, bars, [], 0, dates[-1])
    assert s.issue_cmp == pytest.approx(18.0) and not s.issue_note
    s = GStock(code="X", mkt="us", issue_price=18.0, issue_date="2015-06-01")  # 代码被重复使用
    compute(s, bars, [], 0, dates[-1])
    assert math.isnan(s.issue_cmp) and "对不上" in s.issue_note
    s = GStock(code="X", mkt="hk", issue_price=180.0, issue_date=dates[0])  # 数值不合理
    compute(s, bars, [], 0, dates[-1])
    assert math.isnan(s.issue_cmp) and "相差过大" in s.issue_note
    # 上市后 1 拆 5：首日收盘 100（不复权）→ Yahoo 显示 20；招股价 80 换算后 16
    s = GStock(code="X", mkt="hk", issue_price=80.0, issue_date=dates[0])
    compute(s, bars, [("2024-05-15", 5.0)], 0, dates[-1])
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
    assert set(got) == {"AAPL", "BABA", "BRK.B", "PDI", "ET", "IEP"}
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
    assert got == {"00700": {"name_en": "TENCENT", "board": "主板", "fund": False},
                   "08001": {"name_en": "ORIENT SEC INT", "board": "创业板", "fund": False}}


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
    assert bars[1].close == 2.1 and splits == [("2024-01-04", 2.0)] and n_div == 2


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
            yield code, (bars, splits, 1), ""

    def latest_date(self):
        return self.dates[self.last_idx]

    def quotes(self, syms):
        return {s: {"name": "中文" + s[2:], "price": self.raw(s[2:])[-1][3], "date": self.dates[self.last_idx]} for s in syms}

    def hk_issue_info(self):
        self.calls.append(("hk_issue_info", 0))
        return {c: {"price": 9.0, "list_date": self.dates[0], "name": "东财" + c} for c in self.codes}

    def us_ipo_month(self, ym):
        self.calls.append(("ipo", ym))
        return [{"sym": "S1", "price": 9.0, "date": "2024-02-29", "name": "S1 Corp"}] if ym == "2024-02" else []


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
    assert up.stocks["00002"].last_trade == src.dates[40] and up.stocks["00002"].fails == 1
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
