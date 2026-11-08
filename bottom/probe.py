"""检查各数据源能否访问、返回格式是否符合预期。不改动任何数据。"""

from __future__ import annotations

import logging
import traceback

from .util import add_days, bj_today, summary

log = logging.getLogger(__name__)


def _check(name: str, fn, critical: bool, results: list) -> object:
    try:
        detail, value = fn()
        summary(f"- ✅ {name}：{detail}")
        results.append((name, True, critical))
        return value
    except Exception as e:  # noqa: BLE001
        summary(f"- ❌ {name}：{type(e).__name__}: {e}")
        log.debug(traceback.format_exc())
        results.append((name, False, critical))
        return None


def probe() -> int:
    summary("## 数据源检查")
    summary("")
    results: list = []
    today = bj_today()

    from .src_baostock import BaoStock

    api = _check("BaoStock 登录", lambda: ("成功", BaoStock()), True, results)
    if api is not None:
        def basic():
            rows = api.stock_basic()
            n = sum(1 for r in rows if r.get("type") == "1" and r.get("status") == "1")
            return f"上市股票 {n} 只", rows

        _check("BaoStock 股票列表", basic, True, results)

        latest = _check("BaoStock 最新交易日", lambda: (lambda d: (d, d))(api.latest_date()), True, results)

        def hist():
            bars, adjs = api.history_with_adj("sh.600519", today)
            n_ev = len(set(adjs))
            first, last = bars[0], bars[-1]
            return (f"贵州茅台 {len(bars)} 根K线，{first.date} ~ {last.date}，首日前收盘 {first.preclose}，"
                    f"当前后复权因子 {adjs[-1]:.4f}（共 {n_ev - 1} 次除权）"), (bars, adjs)

        ha = _check("BaoStock 单只历史 + 复权因子", hist, True, results)

        if ha:
            def cmp_hfq():
                # 用 BaoStock 自己的后复权收盘价对照我们算的：比值应接近 1
                bars, adjs = ha
                cols, rows = api._query(
                    __import__("baostock").query_history_k_data_plus, "sh.600519", "date,close",
                    start_date=bars[-1].date, end_date=bars[-1].date, frequency="d", adjustflag="1",
                )
                theirs = float(rows[-1][1])
                ours = bars[-1].close * adjs[-1]
                return f"BaoStock 后复权收盘 {theirs:.2f}，本程序计算 {ours:.2f}，比值 {ours / theirs:.4f}", None

            _check("复权口径核对", cmp_hfq, False, results)

            def preclose_check():
                bars, adjs = ha
                # 除权日的前收盘价是否等于“上一日收盘 × 旧因子 / 新因子”（验证每日增量的备用识别方法）
                ok = tot = 0
                for i in range(1, len(bars)):
                    if adjs[i] != adjs[i - 1] and bars[i].traded:
                        tot += 1
                        expect = bars[i - 1].close * adjs[i - 1] / adjs[i]
                        ok += abs(expect - bars[i].preclose) < 0.02
                return f"{ok}/{tot} 次除权日的前收盘价与复权因子吻合", None

            _check("前收盘价含义核对", preclose_check, False, results)

        if latest:
            def daily():
                bars = api.daily_bars(latest)
                facs = api.daily_factors(latest)
                return f"{latest} 全市场日线 {len(bars)} 条，复权因子 {len(facs)} 条", None

            _check("BaoStock 全市场单日数据", daily, True, results)

        _check("BaoStock 交易日历",
               lambda: (lambda d: (f"近 30 天 {len(d)} 个交易日", d))(api.trade_dates(add_days(today, -30), today)),
               True, results)
        api.close()

    from .src_other import bj_history, bj_stock_list, issue_prices, tencent_quotes

    bjl = _check("北交所股票列表", lambda: (lambda r: (f"{len(r)} 只", r))(bj_stock_list()), False, results)

    def quotes():
        syms = ["sh600519", "sz000001", "sz300750", "sh688981"] + (["bj" + bjl[0]["code"]] if bjl else [])
        q = tencent_quotes(syms)
        lines = [f"{k} {v.name} 现价 {v.price} 昨收 {v.preclose} 高 {v.high} 低 {v.low} {v.date} {v.time} "
                 f"{'格式正常' if v.sane() else '⚠️格式可疑'}" for k, v in q.items()]
        if not q:
            raise RuntimeError("没有返回任何行情")
        return "；".join(lines), q

    _check("腾讯实时行情", quotes, False, results)

    if bjl:
        def bjh():
            code = bjl[0]["code"]
            bars, adjs = bj_history(code, bjl[0]["list_date"])
            return f"{code} {len(bars)} 根K线，{bars[0].date} ~ {bars[-1].date}，上市日 {bjl[0]['list_date']}", None

        _check("腾讯北交所历史", bjh, False, results)

    def ipo():
        p = issue_prices()
        if not p:
            raise RuntimeError("没有取到数据（将改用上市首日前收盘价推断发行价）")
        return f"{len(p)} 只", p

    _check("东方财富发行价（失败时改用首日前收盘价推断）", ipo, False, results)

    def fin():
        import time as _t

        from .engine import fin_period
        from .src_other import financials

        p = fin_period(today)
        t0 = _t.time()
        rows = financials(p)
        if not rows:
            raise RuntimeError(f"{p} 报告期没有数据")
        m = {r["code"]: r for r in rows}.get("600519", {})
        sample = (f"贵州茅台：行业 {m.get('industry', '—')}，每股净资产 {m.get('bps')}，净资产收益率 {m.get('roe')}%，"
                  f"毛利率 {m.get('gpm')}%，营收同比 {m.get('rev_yoy')}%，净利润同比 {m.get('np_yoy')}%") if m else "没有贵州茅台"
        return f"{p} 报告期 {len(rows)} 只（用时 {_t.time() - t0:.0f} 秒）；{sample}", rows

    _check("东方财富业绩报表（质量分、成长分、行业）", fin, False, results)

    bad_critical = [n for n, ok, c in results if not ok and c]
    bad_other = [n for n, ok, c in results if not ok and not c]
    summary("")
    if bad_critical:
        summary(f"**关键数据源不可用：{'、'.join(bad_critical)}。网页无法更新。**")
        return 1
    if bad_other:
        summary(f"部分数据源不可用：{'、'.join(bad_other)}。沪深数据不受影响，相关功能会降级。")
    else:
        summary("全部正常，可以运行 rebuild 做首次全量构建。")
    return 0
