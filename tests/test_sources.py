"""数据源解析逻辑测试（用假的 BaoStock 返回值，不联网）。"""

from __future__ import annotations

import math

import pytest

import bottom.src_baostock as sb
from bottom import pipeline
from bottom.src_other import parse_quotes
from tests.fakeworld import FakeSources, World
from tests.test_logic import run_day
from bottom import store


class FakeRS:
    def __init__(self, fields, rows, page=2):
        self.error_code, self.error_msg = "0", ""
        self.fields = fields
        self._rows = rows
        self._i = 0

    def next(self):
        return self._i < len(self._rows)

    def get_row_data(self):
        r = self._rows[self._i]
        self._i += 1
        return r


@pytest.fixture()
def api(monkeypatch):
    class L:
        error_code, error_msg = "0", ""

    monkeypatch.setattr(sb.bs, "login", lambda *a, **k: L())
    monkeypatch.setattr(sb.bs, "logout", lambda *a, **k: None)
    return sb.BaoStock()


def test_history_with_adj(api, monkeypatch):
    k_fields = "date,open,high,low,close,preclose,volume,tradestatus".split(",")
    k_rows = [
        ["2020-01-02", "10", "11", "9.5", "10.5", "10", "100", "1"],
        ["2020-01-03", "10.5", "10.6", "10.1", "10.2", "10.5", "100", "1"],
        ["2020-01-06", "10.2", "10.2", "10.2", "10.2", "10.2", "0", "0"],  # 停牌
        ["2020-01-07", "5.0", "5.2", "4.9", "5.1", "5.1", "100", "1"],  # 10 送 10
    ]
    f_fields = "code,dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFactor".split(",")
    f_rows = [["sh.600000", "2020-01-07", "0.5", "2.0", "2.0"]]
    monkeypatch.setattr(sb.bs, "query_history_k_data_plus", lambda *a, **k: FakeRS(k_fields, k_rows))
    monkeypatch.setattr(sb.bs, "query_adjust_factor", lambda *a, **k: FakeRS(f_fields, f_rows))
    bars, adjs = api.history_with_adj("sh.600000", "2020-01-07")
    assert [b.traded for b in bars] == [True, True, False, True]
    assert adjs == [1.0, 1.0, 1.0, 2.0]
    assert bars[0].preclose == 10.0


def test_daily_factors_ignores_future(api, monkeypatch):
    f_fields = "code,dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFactor".split(",")
    rows = [
        ["sh.600000", "2024-06-01", "1", "1.5", "1"],
        ["sh.600000", "2024-07-01", "1", "1.6", "1"],
        ["sh.600000", "2024-08-01", "1", "1.9", "1"],  # 未来日期，不能提前用
        ["sz.000001", "2024-07-01", "1", "", "1"],  # 缺值
    ]
    monkeypatch.setattr(sb.bs, "query_daily_adjust_factor", lambda *a, **k: FakeRS(f_fields, rows))
    assert api.daily_factors("2024-07-01") == {"600000": 1.6}


def test_query_retries_then_raises(api, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise ConnectionError("x")

    boom.__name__ = "query_history_k_data_plus"
    monkeypatch.setattr(sb.bs, "query_history_k_data_plus", boom)
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    with pytest.raises(sb.BaoStockError):
        api.history("sh.600000", "2024-01-01")
    assert len(calls) == api.retries


def test_parse_tencent_quote():
    f = ["1", "贵州茅台", "600519", "1500.00", "1490.00", "1495.00", "12345"] + ["0"] * 23
    f += ["20261106150003", "10.00", "0.67", "1510.00", "1488.00"] + ["0"] * 10
    text = 'v_sh600519="' + "~".join(f) + '";\n'
    q = parse_quotes(text)["sh600519"]
    assert (q.price, q.preclose, q.high, q.low, q.date, q.time) == (1500.0, 1490.0, 1510.0, 1488.0, "2026-11-06", "15:00:03")
    assert q.sane()
    b = q.to_bar()
    assert b.traded and b.close == 1500.0


def test_bad_day_is_rejected(tmp_path, monkeypatch):
    """前收盘价大面积对不上（数据源出错）时，放弃当天更新，状态不变。"""
    monkeypatch.setattr(pipeline, "EVENT_CHECK_MIN", 1)
    w = World()
    d = str(tmp_path)
    run_day(w, 30, d)
    before, _ = store.load(d)

    class Broken(FakeSources):
        def daily_bars(self, date):
            out = super().daily_bars(date)
            for code, (m, b, st, val) in out.items():
                b = type(b)(**{**b.__dict__, "preclose": b.preclose * 1.1})
                out[code] = (m, b, st, val)
            return out

        def daily_factors(self, date):
            return {}

    stocks, meta = store.load(d)
    up = pipeline.Updater(Broken(w, 31), stocks, meta, today=w.cal[31], now_hm="20:00")
    up.daily_shsz()
    assert up.meta["bs_date"] == w.cal[30]
    for code, s in before.items():
        if s.mkt != "bj":
            assert up.stocks[code].to_record() == s.to_record()
    assert any("放弃" in n for n in up.notes)


def test_eastmoney_financials_paging_and_parsing(monkeypatch):
    import bottom.src_other as so

    names = ["SECURITY_CODE", "SECURITY_NAME_ABBR", "TRADE_MARKET_CODE", "TRADE_MARKET", "SECURITY_TYPE_CODE",
             "SECURITY_TYPE", "UPDATE_DATE", "REPORTDATE", "BASIC_EPS", "DEDUCT_BASIC_EPS", "TOTAL_OPERATE_INCOME",
             "PARENT_NETPROFIT", "WEIGHTAVG_ROE", "YSTZ", "SJLTZ", "BPS", "MGJYXJJE", "XSMLL", "YSHZ", "SJLHZ",
             "ASSIGNDSCRPT", "PAYYEAR", "PUBLISHNAME"]

    def row(code, roe, ind, rename=False):
        vals = [code, "名", "", "", "", "", "", "", 1, 1, 1, 1, roe, 5.0, 6.0, 10.0, 0.8, 30.0, 0, 0, "", "", ind]
        keys = [f"F{i}" for i in range(len(names))] if rename else names
        return dict(zip(keys, vals))

    pages = {1: [row("600519", 25.0, "酿酒行业"), row("1", 8.0, None)], 2: [row("920001", 5.0, "专用设备")]}
    calls = []
    fails = {"n": 1}

    class Resp:
        def __init__(self, p):
            self.p = p

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"pages": 2, "data": pages[self.p]}}

    def fake_get(url, params=None, **kw):
        calls.append(params["pageNumber"])
        if fails["n"]:
            fails["n"] -= 1
            raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")
        return Resp(int(params["pageNumber"]))

    import requests
    monkeypatch.setattr(so.requests, "get", fake_get)
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    recs = {r["code"]: r for r in so.financials("2026-09-30")}
    assert calls == ["1", "1", "2"]  # 第一次被中途断开，重试后成功
    assert recs["600519"]["roe"] == 25.0 and recs["600519"]["bps"] == 10.0 and recs["600519"]["gpm"] == 30.0
    assert recs["600519"]["ocfps"] == 0.8 and recs["600519"]["rev_yoy"] == 5.0 and recs["600519"]["np_yoy"] == 6.0
    assert recs["000001"]["industry"] == "" and recs["920001"]["industry"] == "专用设备"
    # 字段名变了：按顺序读取，结果一样
    assert so._em_rows_to_records([row("600519", 25.0, "酿酒行业", rename=True)])[0]["bps"] == 10.0


def test_eastmoney_gives_up_after_retries(monkeypatch):
    import requests
    import bottom.src_other as so

    def boom(*a, **k):
        raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")

    monkeypatch.setattr(so.requests, "get", boom)
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        so.financials("2026-09-30")
