"""用模拟市场验证：每日增量、全量重建、补跑、缺数据、停牌期间除权、新股、发行价。"""

from __future__ import annotations

import json
import math
import os

import pytest

from bottom import store
from bottom.engine import Bar, Stock, apply_bar
from bottom.export import export
from bottom.pipeline import Updater
from tests.fakeworld import FakeSources, World


def run_day(world, idx, state_dir, mode="daily", **kw):
    stocks, meta = store.load(state_dir)
    src = FakeSources(world, idx, **kw)
    up = Updater(src, stocks, meta, today=world.cal[idx], now_hm="20:00")
    up.run(mode)
    store.save(up.stocks, up.meta, state_dir)
    return up, src


def check_truth(world, stocks, upto_idx, tol=1e-6):
    for code, fs in world.stocks.items():
        if fs.list_idx > upto_idx:
            assert code not in stocks or not stocks[code].has_data()
            continue
        s = stocks[code]
        exp = world.truth(code, upto_idx)
        m = s.metrics()
        assert m is not None, code
        assert m["ratio_low"] == pytest.approx(exp[0], rel=tol), (code, m, exp)
        assert m["ratio_close"] == pytest.approx(exp[1], rel=tol), (code, m, exp)


@pytest.fixture()
def world():
    return World()


def test_daily_incremental_matches_truth(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 20, d)  # 第一次运行：自动全量构建
    for i in range(21, len(world.cal)):
        up, _ = run_day(world, i, d)
        if i in (41, 61, 81, 91, 101, 131, 141):  # 各种除权、复牌之后抽查
            check_truth(world, up.stocks, i)
    stocks, meta = store.load(d)
    check_truth(world, stocks, len(world.cal) - 1)
    assert meta["bs_date"] == world.cal[-1]
    # 送转股、分红股确实识别到了除权
    assert stocks["300003"].n_events == 2
    assert stocks["000002"].n_events == 3
    assert stocks["920007"].n_events == 2  # 北交所靠前收盘价识别


def test_rebuild_equals_incremental(world, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    run_day(world, 10, a)
    for i in range(11, 150):
        run_day(world, i, a)
    up_b, _ = run_day(world, 149, b, mode="rebuild")
    sa, _ = store.load(a)
    for code, s in up_b.stocks.items():
        if not s.has_data():
            continue
        assert s.metrics()["ratio_low"] == pytest.approx(sa[code].metrics()["ratio_low"], rel=1e-9), code
        assert s.min_low_date == sa[code].min_low_date


def test_gap_catch_up(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 30, d)
    run_day(world, 31, d)
    up, src = run_day(world, 45, d)  # 中间漏跑 13 天，按天补齐
    assert ("daily_bars", world.cal[32]) in src.calls
    check_truth(world, up.stocks, 45)
    up, src = run_day(world, 70, d)  # 漏跑 25 天，超过上限，改为全量构建
    assert any(c[0] == "history" for c in src.calls)
    check_truth(world, up.stocks, 70)


def test_baostock_lag(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 30, d)
    up, _ = run_day(world, 31, d, bs_lag=1)  # BaoStock 当天还没入库
    assert up.meta["bs_date"] == world.cal[30]
    up, _ = run_day(world, 31, d)  # 晚些时候再跑一次就补上了
    assert up.meta["bs_date"] == world.cal[31]
    check_truth(world, up.stocks, 31)


def test_missing_bar_triggers_rebuild(world, tmp_path, monkeypatch):
    # 模拟市场只有 6 只沪深股票，缺 1 只就低于“入库完整”的 95%；这里放宽，专门测缺数据的处理
    import bottom.pipeline as pl
    monkeypatch.setattr(pl, "COMPLETE_RATIO", 0.8)
    d = str(tmp_path)
    run_day(world, 95, d)
    for i in range(96, 101):
        run_day(world, i, d)
    up, src = run_day(world, 101, d, drop={("000002", 100), ("000002", 101)})
    # 99、100 天正常；缺数据时那只股票改用完整历史重算
    assert ("history", "000002") in src.calls
    check_truth(world, up.stocks, 101)


def test_suspended_through_ex_date(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 60, d)
    for i in range(61, 95):
        run_day(world, i, d)
    stocks, _ = store.load(d)
    s = stocks["002005"]
    assert s.n_events == 1  # 停牌期间除权只算一次，复牌日不重复
    check_truth(world, stocks, 94)


def test_new_listing_and_issue_price(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 115, d)
    for i in range(116, 125):
        run_day(world, i, d)
    stocks, _ = store.load(d)
    f = stocks["601006"]
    assert f.first_date == world.cal[120]
    assert f.issue_src == "em" and f.issue_price == 12.0
    a = stocks["600001"]  # 没有东方财富数据，用首日前收盘推断
    assert a.issue_src == "infer" and a.issue_price == pytest.approx(3.0)
    d_ = stocks["688004"]  # 首日破发：历史最低不是发行价
    assert d_.metrics()["low_date"] != "发行价"
    # 600001 上市以来一直没跌破发行价 3 元？由真实答案决定，这里只比对数值
    check_truth(world, stocks, 124)


def test_preclose_mismatch_rejected():
    s = Stock(code="600000", mkt="sh")
    apply_bar(s, Bar("2024-01-02", 10, 10.5, 9.8, 10.2, 10.0))
    apply_bar(s, Bar("2024-01-03", 10.2, 10.4, 10.0, 10.3, 10.2))
    assert s.adj == 1.0
    # 10 送 10：前收盘参考价 5.15
    r = apply_bar(s, Bar("2024-01-04", 5.2, 5.3, 5.1, 5.2, 5.15))
    assert r == "event" and s.adj == pytest.approx(2.0)
    assert s.min_low == pytest.approx(9.8)  # 5.1 × 2 = 10.2 > 9.8
    # 重复处理同一天不会重复计算
    assert apply_bar(s, Bar("2024-01-04", 5.2, 5.3, 5.1, 5.2, 5.15)) == "skip"


def test_export_files(world, tmp_path):
    d = str(tmp_path / "state")
    run_day(world, 60, d)
    stocks, meta = store.load(d)
    site = str(tmp_path / "site")
    info = export(stocks, meta, site_dir=site, web_dir="web")
    doc = json.load(open(os.path.join(site, "data", "latest.json"), encoding="utf-8"))
    assert info["rows"] == len(doc["rows"]) == 6  # 新股 F 还没上市
    assert doc["fields"][0] == "code"
    ratios = []
    F = doc["fields"]
    for r in doc["rows"]:
        rec = dict(zip(F, r))
        base = min(rec["min_low"], rec["issue_price"] or math.inf)
        ratios.append(rec["hfq"] / base)
    assert ratios == sorted(ratios)
    csv_text = open(os.path.join(site, "data", "latest.csv"), encoding="utf-8-sig").read()
    assert csv_text.startswith("代码,名称")


def test_failed_stock_recovers_next_run(world, tmp_path):
    """全量构建时某只股票下载失败，之后的每日运行会自动用完整历史补上。"""
    d = str(tmp_path)

    class Flaky(FakeSources):
        def fetch_shsz(self, items, end):
            for code, mkt in items:
                if code == "000002":
                    yield code, None, None, "network"
                else:
                    bars, adjs = self.history_shsz(code, mkt, end)
                    yield code, bars, adjs, ""

        def history_shsz(self, code, mkt, end):
            if code == "000002":
                raise ConnectionError("network")
            return super().history_shsz(code, mkt, end)

    stocks, meta = store.load(d)
    up = Updater(Flaky(world, 50), stocks, meta, today=world.cal[50], now_hm="20:00")
    up.run("daily")
    store.save(up.stocks, up.meta, d)
    assert not up.stocks["000002"].has_data()
    up, src = run_day(world, 51, d)
    assert ("history", "000002") in src.calls
    check_truth(world, up.stocks, 51)


def test_universe_new_and_delisted():
    """新股自动加入；退市（BaoStock 标记 status=0 或从列表消失）自动剔除；列表明显不全时不删。"""
    from bottom.engine import Stock as St

    class Src:
        def __init__(self, rows, bj):
            self.rows, self.bj = rows, bj

        def stock_basic(self):
            return self.rows

        def bj_stock_list(self):
            if self.bj is None:
                raise ConnectionError("bse.cn down")
            return self.bj

    def row(code, status="1", ipo="2020-01-02", name="股"):
        return {"code": code, "code_name": name, "ipoDate": ipo, "outDate": "", "type": "1", "status": status}

    base = [row(f"sh.6000{i:02d}") for i in range(30)]
    stocks = {f"6000{i:02d}": St(code=f"6000{i:02d}", mkt="sh", built="x") for i in range(30)}
    stocks["920001"] = St(code="920001", mkt="bj", built="x")
    # 600005 退市（status=0），600007 从列表消失，688999 新上市，688998 尚未到上市日
    rows = [r for r in base if r["code"] not in ("sh.600007",)]
    rows = [row(r["code"], status="0") if r["code"] == "sh.600005" else r for r in rows]
    rows += [row("sh.688999", ipo="2024-03-01"), row("sh.688998", ipo="2024-03-10")]
    up = Updater(Src(rows, None), stocks, {}, today="2024-03-05")
    up.refresh_universe()
    assert "600005" not in up.stocks and "600007" not in up.stocks
    assert "688999" in up.stocks and up.stocks["688999"].built == ""  # 待用完整历史构建
    assert "688998" not in up.stocks
    assert "920001" in up.stocks  # 北交所列表获取失败时不删北交所股票
    # 列表明显不全（只返回 10 只）时一只都不删
    before = set(up.stocks)
    up2 = Updater(Src(base[:10], []), dict(up.stocks), {}, today="2024-03-05")
    up2.refresh_universe()
    assert before <= set(up2.stocks)  # 北交所列表为空也不删


def test_weekly_verify_rebuilds_only_mismatched(world, tmp_path):
    """每周核对：复权因子对得上的不动，对不上的用完整历史重算。"""
    d = str(tmp_path)
    run_day(world, 110, d)
    for i in range(111, 122):
        run_day(world, i, d)
    stocks, meta = store.load(d)
    # 模拟某次漏掉除权：本地因子整体偏了 1%
    stocks["000002"].adj *= 1.01
    stocks["000002"].last_adj *= 1.01
    store.save(stocks, meta, d)
    up, src = run_day(world, 121, d, mode="verify")
    hist = {c for k, c in src.calls if k == "history"}
    assert hist == {"000002"}, hist
    assert {c for k, c in src.calls if k == "factors"} >= {"600001", "000002", "300003"}
    assert ("bj_history", "920007") in src.calls  # 北交所直接重算
    check_truth(world, up.stocks, 121)


def test_build_end_waits_for_complete_ingestion(world, tmp_path):
    """BaoStock 当天只入库了一部分：全量构建截止到上一个交易日，之后再补。"""
    d = str(tmp_path)

    class Partial(FakeSources):
        def daily_bars(self, date):
            out = super().daily_bars(date)
            if date == self.today:
                out = dict(list(out.items())[:2])
            return out

    stocks, meta = store.load(d)
    up = Updater(Partial(world, 40), stocks, meta, today=world.cal[40], now_hm="20:00")
    up.run("daily")
    store.save(up.stocks, up.meta, d)
    assert up.meta["bs_date"] == world.cal[39]
    up, _ = run_day(world, 40, d)  # 入库完整后补上当天
    assert up.meta["bs_date"] == world.cal[40]
    check_truth(world, up.stocks, 40)


def test_stock_missing_latest_bar_heals(world, tmp_path):
    """全量构建时某只股票最新一天还没入库：不强行标记为已处理，下次运行自动补算。"""
    d = str(tmp_path)

    class Lagging(FakeSources):
        def history_shsz(self, code, mkt, end):
            bars, adjs = super().history_shsz(code, mkt, end)
            if code == "000002" and bars and bars[-1].date == end:
                bars, adjs = bars[:-1], adjs[:-1]
            return bars, adjs

    stocks, meta = store.load(d)
    up = Updater(Lagging(world, 50), stocks, meta, today=world.cal[50], now_hm="20:00")
    up.run("daily")
    store.save(up.stocks, up.meta, d)
    assert up.stocks["000002"].last_seen == world.cal[49]
    up, src = run_day(world, 51, d)
    assert ("history", "000002") in src.calls
    check_truth(world, up.stocks, 51)


def test_value_scores_ranking():
    from bottom.engine import Stock as St, value_scores

    def mk(code, pe, pb, ps):
        return St(code=code, mkt="sh", last_close=10.0, min_low=5.0, min_close=5.0, pe=pe, pb=pb, ps=ps)

    nan = math.nan
    ss = [
        mk("600001", 5, 0.6, 0.5),     # 三项都最便宜
        mk("600002", 15, 1.5, 2.0),
        mk("600003", 60, 6.0, 8.0),    # 很贵
        mk("600004", -8, 2.0, 1.0),    # 亏损：市盈率为负，应排在市盈率维度最差
        mk("600005", 20, nan, nan),    # 只有一个指标：不给分
        mk("600006", 25, -1.0, 3.0),   # 资不抵债：市净率为负
    ]
    sc = value_scores(ss)
    assert "600005" not in sc
    assert sc["600001"] == pytest.approx(100.0)
    assert sc["600001"] > sc["600002"] > sc["600003"]
    assert all(0 <= v <= 100 for v in sc.values())
    # 亏损股的市盈率百分位是 0（最差）
    sc_pe_only = value_scores([mk(c, pe, 1.0, 1.0) for c, pe in [("1", 5), ("2", 50), ("3", -8)]])
    assert sc_pe_only["3"] < sc_pe_only["2"] < sc_pe_only["1"]


def test_valuation_flows_into_export(world, tmp_path):
    d = str(tmp_path / "state")
    run_day(world, 60, d)            # 首次构建（没有逐日数据）：估值靠“补取一次”
    up, src = run_day(world, 61, d)  # 每日增量：估值随全市场数据写入
    s = up.stocks["000002"]
    assert s.val_date == world.cal[61] and s.pe == pytest.approx(s.last_close / 0.8)
    assert up.stocks["688004"].pe < 0  # 亏损股
    site = str(tmp_path / "site")
    export(up.stocks, up.meta, site_dir=site, web_dir="web")
    doc = json.load(open(os.path.join(site, "data", "latest.json"), encoding="utf-8"))
    F = doc["fields"]
    rows = {r[0]: dict(zip(F, r)) for r in doc["rows"]}
    assert rows["000002"]["vscore"] is not None and rows["688004"]["vscore"] is not None
    assert rows["920007"]["vscore"] is None  # 模拟市场沪深股票太少，北交所不做字段核对，也就没有分
    # 亏损的 688004 市盈率维度最差，总分应低于盈利且更便宜的 600001
    assert rows["688004"]["vscore"] < rows["600001"]["vscore"]


def test_first_run_fills_valuation(world, tmp_path):
    d = str(tmp_path)
    up, src = run_day(world, 60, d)
    assert up.stocks["600001"].val_date == world.cal[60]
    assert any("估值：补充沪深" in n for n in up.notes)


def test_bj_valuation_field_check():
    """北交所估值：先用沪深股票核对腾讯字段，选出和 BaoStock 一致的那个；都对不上就不用。"""
    from bottom.engine import Stock as St
    from bottom.src_other import Quote

    def mkq(sym, pe39, pe52, pb46):
        raw = ["0"] * 60
        raw[39], raw[52], raw[46] = str(pe39), str(pe52), str(pb46)
        return Quote(sym, "", 10, 10, 10, 10, 10, 100, "2024-03-01", "15:00:00", tuple(raw))

    stocks, quotes = {}, {}
    for i in range(12):
        code = f"6000{i:02d}"
        s = St(code=code, mkt="sh", last_close=10, min_low=5, min_close=5, pe=10 + i, pb=1 + i / 10, val_date="2024-03-01")
        stocks[code] = s
        # 第 39 个字段是动态市盈率（和 TTM 不同），第 52 个才和 BaoStock 一致
        quotes["sh" + code] = mkq("sh" + code, (10 + i) * 1.3, (10 + i) * 1.004, (1 + i / 10) * 0.998)
    stocks["920001"] = St(code="920001", mkt="bj", last_close=10, min_low=5, min_close=5)
    quotes["bj920001"] = mkq("bj920001", 99, 33.0, 2.5)

    class Src:
        def quotes(self, syms):
            return {k: v for k, v in quotes.items() if k in syms}

    up = Updater(Src(), stocks, {"bs_date": "2024-03-01"}, today="2024-03-02")
    up.refresh_bj_valuation()
    assert stocks["920001"].pe == 33.0 and stocks["920001"].pb == 2.5
    # 所有候选字段都对不上：不写入
    for k, q in list(quotes.items()):
        if k.startswith("sh"):
            raw = list(q.raw)
            raw[39] = raw[52] = raw[53] = "123"
            quotes[k] = Quote(q.sym, "", 10, 10, 10, 10, 10, 100, q.date, q.time, tuple(raw))
    stocks["920001"].pe = math.nan
    up.refresh_bj_valuation()
    assert math.isnan(stocks["920001"].pe)
    assert any("对不上" in n for n in up.notes)


# ---------------- 评分：行业内比较、质量、成长、综合 ----------------
def _st(code, industry="", pe=math.nan, pb=math.nan, ps=math.nan, **kw):
    from bottom.engine import Stock as St
    return St(code=code, mkt="sh", last_close=10.0, min_low=5.0, min_close=5.0,
              industry=industry, pe=pe, pb=pb, ps=ps, **kw)


def test_industry_neutral_value():
    from bottom.engine import compute_scores
    banks = [_st(f"60{i:04d}", "银行", pe=4 + i * 0.4, pb=0.5 + i * 0.05, ps=1 + i * 0.1) for i in range(10)]
    tech = [_st(f"30{i:04d}", "半导体", pe=30 + i * 5, pb=3 + i * 0.5, ps=5 + i) for i in range(10)]
    sc = compute_scores(banks + tech)
    # 每个行业里最便宜的都是 100 分，最贵的都是 0 分——不再是“银行永远便宜、科技永远贵”
    assert sc["600000"]["value"] == pytest.approx(100) and sc["300000"]["value"] == pytest.approx(100)
    assert sc["600009"]["value"] == pytest.approx(0) and sc["300009"]["value"] == pytest.approx(0)


def test_small_industry_falls_back_to_market():
    from bottom.engine import compute_scores, MIN_GROUP
    big = [_st(f"60{i:04d}", "银行", pe=5 + i, pb=1 + i / 10, ps=1 + i / 5) for i in range(MIN_GROUP)]
    small = [_st("688001", "小行业", pe=100, pb=10, ps=20), _st("688002", "小行业", pe=200, pb=20, ps=40)]
    sc = compute_scores(big + small)
    # 小行业只有 2 只：不在组内排（否则 688001 会是 100 分），而是和全市场比，排在最后
    assert sc["688001"]["value"] < 15 and sc["688002"]["value"] == pytest.approx(0)


def test_quality_growth_total():
    from bottom.engine import compute_scores, TOTAL_WEIGHTS
    ss = []
    for i in range(10):
        ss.append(_st(f"60{i:04d}", "银行", pe=5 + i, pb=0.6 + i / 10, ps=2 + i / 5,
                      roe=5 + i, gpm=math.nan, ocfps=1 + i / 10, bps=8.0, rev_yoy=i - 3.0, np_yoy=2 * i - 5.0))
    ss.append(_st("600099", "银行", pe=6, pb=0.7, ps=2))  # 没有财务数据
    sc = compute_scores(ss)
    assert "quality" in sc["600009"]  # 银行没有毛利率，用 ROE 和 经营现金流/净资产 两项
    assert sc["600009"]["quality"] > sc["600000"]["quality"]
    assert sc["600009"]["growth"] > sc["600000"]["growth"]
    d = sc["600005"]
    assert d["total"] == pytest.approx(sum(d[k] * w for k, w in TOTAL_WEIGHTS.items()))
    assert "total" not in sc["600099"] and "value" in sc["600099"]  # 缺质量/成长就不给综合分


def test_fin_period():
    from bottom.engine import fin_period
    assert fin_period("2026-11-08") == "2026-09-30"
    assert fin_period("2026-11-02") == "2026-06-30"
    assert fin_period("2026-09-10") == "2026-06-30"
    assert fin_period("2026-06-01") == "2026-03-31"
    assert fin_period("2027-02-01") == "2026-09-30"
    assert fin_period("2027-05-05") == "2027-03-31"


def _fin_updater(rows_factory, n=40):
    stocks = {}
    for i in range(n):
        code = f"60{i:04d}"
        stocks[code] = _st(code, pe=10 + i, pb=1.0 + i / 20, ps=2.0, last_trade="2026-11-06", val_date="2026-11-06")

    class Src:
        calls = 0

        def financials(self, period):
            Src.calls += 1
            return rows_factory(stocks)

    return Updater(Src(), stocks, {}, today="2026-11-08"), Src


def test_financials_accepted_and_cached():
    def good(stocks):
        return [{"code": c, "industry": "银行", "bps": s.last_close / s.pb, "roe": 9.0, "gpm": 30.0,
                 "ocfps": 0.5, "rev_yoy": 5.0, "np_yoy": 3.0} for c, s in stocks.items()]
    up, Src = _fin_updater(good)
    up.refresh_financials()
    s = up.stocks["600001"]
    assert s.industry == "银行" and s.fin_period == "2026-09-30" and s.roe == 9.0
    assert any("核对通过" in n for n in up.notes)
    up.refresh_financials()  # 7 天内不重复下载
    assert Src.calls == 1


def test_financials_rejected_when_columns_shift():
    def shifted(stocks):  # 模拟数据源改格式：“每股净资产”一列放的其实是别的数
        return [{"code": c, "industry": "银行", "bps": 3.3, "roe": 9.0, "gpm": 30.0,
                 "ocfps": 0.5, "rev_yoy": 5.0, "np_yoy": 3.0} for c, s in stocks.items()]
    up, _ = _fin_updater(shifted)
    up.stocks["600001"].roe = 7.0
    up.stocks["600001"].fin_period = "2026-06-30"
    up.refresh_financials()
    assert up.stocks["600001"].roe == 7.0  # 旧数据保留
    assert any("没有通过核对" in n for n in up.notes)

    def few(stocks):
        return [{"code": "600001", "industry": "", "bps": 1, "roe": 1, "gpm": 1, "ocfps": 1, "rev_yoy": 1, "np_yoy": 1}]
    up2, _ = _fin_updater(few)
    up2.refresh_financials()
    assert any("只覆盖" in n for n in up2.notes)


def test_scores_end_to_end(world, tmp_path):
    d = str(tmp_path / "state")
    _, src0 = run_day(world, 60, d)
    assert ("financials", "2024-03-31") in src0.calls or ("financials", "2023-09-30") in src0.calls
    up, src = run_day(world, 61, d)
    assert not any(k == "financials" for k, _ in src.calls)  # 一周内不重复下载
    s = up.stocks["000002"]
    assert s.industry == "银行" and math.isfinite(s.roe)
    site = str(tmp_path / "site")
    export(up.stocks, up.meta, site_dir=site, web_dir="web")
    doc = json.load(open(os.path.join(site, "data", "latest.json"), encoding="utf-8"))
    F = doc["fields"]
    rows = {r[0]: dict(zip(F, r)) for r in doc["rows"]}
    for k in ("vscore", "qscore", "gscore", "tscore", "industry"):
        assert k in F
    assert rows["000002"]["tscore"] is not None


def test_rebuild_keeps_valuation_industry_financials(world, tmp_path):
    d = str(tmp_path)
    run_day(world, 60, d)
    up, _ = run_day(world, 61, d)
    before = {c: (s.pe, s.industry, s.roe, s.fin_period) for c, s in up.stocks.items()}
    up2, src = run_day(world, 61, d, mode="rebuild")  # 完整重算价格历史
    for c, s in up2.stocks.items():
        assert (s.pe, s.industry, s.roe, s.fin_period) == before[c] or math.isnan(before[c][0])
    assert up2.stocks["000002"].industry == "银行" and math.isfinite(up2.stocks["000002"].roe)


def test_delisting_stocks_not_scored():
    from bottom.engine import compute_scores, is_delisting
    assert is_delisting("萃华退") and is_delisting("退市海润") and not is_delisting("*ST岭南") and not is_delisting("退婴科技")
    ss = [_st(f"60{i:04d}", "化工", pe=10 + i, pb=1 + i / 10, ps=1 + i / 10) for i in range(10)]
    ss.append(_st("002731", "化工", pe=0.2, pb=0.03, ps=0.05))
    ss[-1].name = "萃华退"
    sc = compute_scores(ss)
    assert "002731" not in sc
    assert sc["600000"]["value"] == pytest.approx(100)  # 最便宜的正常股票仍是 100 分，没被退市股挤掉


def test_f10_issue_prices(world, tmp_path):
    d = str(tmp_path)
    f10 = {
        "600001": {"price": 3.0, "first_close": 3.3, "swap": False},  # 和首日前收盘一致
        "000002": {"price": 8.0, "first_close": 8.1, "swap": True},  # 换股上市：没有发行价
        "300003": {"price": 19.5, "first_close": 25.0, "swap": False},  # 和首日前收盘（20）不一致，以发行资料为准
        "002005": {"price": 600.0, "first_close": 6.0, "swap": False},  # 数值不合理，不采用
        "688004": {"price": 1.0, "first_close": 50.0, "swap": False},  # 已有东方财富新股数据，不覆盖
        "999999": {"price": 5.0, "first_close": 5.0, "swap": False},
        "920007": {"price": 9.0, "first_close": 9.5, "swap": False, "list_date": "2001-01-01"},  # 上市日期对不上
    }
    up, src = run_day(world, 125, d, f10=f10)
    st = up.stocks
    assert (st["600001"].issue_src, st["600001"].issue_price) == ("f10", 3.0)
    assert st["000002"].issue_src == "none" and not st["000002"].issue_price == st["000002"].issue_price
    assert st["000002"].metrics()["low_date"] != "发行价"
    assert (st["300003"].issue_src, st["300003"].issue_price) == ("f10", 19.5)
    assert (st["002005"].issue_src, st["002005"].issue_price) == ("infer", 6.0)
    assert (st["688004"].issue_src, st["688004"].issue_price) == ("em", 50.0)
    assert st["920007"].issue_src == "em"  # 北交所已有东方财富新股数据
    assert any("不一致" in n and "300003" in n for n in up.notes)
    assert up.meta["f10_issue_date"] == world.cal[125]
    # 一周内不再请求；换股上市的标记在全量重建后仍然保留，不会被重新推断
    up, src = run_day(world, 126, d, f10=f10)
    assert ("f10", None) not in src.calls
    up, src = run_day(world, 127, d, mode="rebuild", f10=None)
    assert up.stocks["000002"].issue_src == "none"
    assert up.stocks["600001"].issue_src == "f10"
    assert any("发行资料获取失败" in n for n in up.notes)


def test_f10_incomplete_not_used(world, tmp_path):
    d = str(tmp_path)
    up, _ = run_day(world, 125, d, f10={"600001": {"price": 1.0, "first_close": 3.0, "swap": True}})
    assert up.stocks["600001"].issue_src == "infer"
    assert "f10_issue_date" not in up.meta


def test_f10_listing_date_mismatch_rejected(world, tmp_path):
    d = str(tmp_path)
    f10 = {c: {"price": fs.issue, "first_close": fs.issue, "swap": False} for c, fs in world.stocks.items()}
    f10["600001"] = {"price": 2.0, "first_close": 2.1, "swap": False, "list_date": "1995-01-01"}  # 代码被重复使用
    up, _ = run_day(world, 125, d, f10=f10)
    assert up.stocks["600001"].issue_src == "infer" and up.stocks["600001"].issue_price == 3.0
    assert up.stocks["000002"].issue_src == "f10"
