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


def test_missing_bar_triggers_rebuild(world, tmp_path):
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
