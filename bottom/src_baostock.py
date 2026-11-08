"""BaoStock 数据源（沪深两市）。免费、无需注册。

用到的接口：
* query_stock_basic          全部证券的上市日期、上市状态
* query_trade_dates          交易日历
* query_history_k_data_plus  单只股票的历史日线（不复权，含前收盘价）
* query_adjust_factor        单只股票的复权因子（BaoStock 用等比/涨跌幅复权）
* query_daily_history_k_AStock  某一天全部 A 股的日线（每日增量用，一次请求）
* query_daily_adjust_factor     某一天的复权因子（每日增量用，一次请求）

注意：BaoStock 自带的 get_data() 用了新版 pandas 已删除的 DataFrame.append，
所以这里统一用 next()/get_row_data() 逐行读取。
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import socket
import time

import baostock as bs

from .engine import Bar
from .util import add_days, bj_today

log = logging.getLogger(__name__)

# BaoStock 的 socket 没有超时，网络卡住会一直等；这里统一设个超时
socket.setdefaulttimeout(60)

HIST_START = "1990-01-01"


class BaoStockError(RuntimeError):
    pass


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def to_bs_code(code: str, mkt: str) -> str:
    return f"{mkt}.{code}"


class BaoStock:
    def __init__(self, retries: int = 4):
        self.retries = retries
        self._login()

    # ---------- 连接 ----------
    def _login(self) -> None:
        last = ""
        for i in range(5):
            with contextlib.redirect_stdout(io.StringIO()):  # 屏蔽 login success 之类的打印
                lg = bs.login()
            if lg.error_code == "0":
                return
            last = f"{lg.error_code} {lg.error_msg}"
            time.sleep(3 * (i + 1))
        raise BaoStockError(f"BaoStock 登录失败：{last}")

    def close(self) -> None:
        with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
            bs.logout()

    def _query(self, fn, *args, **kwargs) -> tuple[list[str], list[list[str]]]:
        """执行查询并读出全部行；出错时重新登录后重试。"""
        err = None
        for attempt in range(self.retries):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rs = fn(*args, **kwargs)
                    if rs.error_code != "0":
                        raise BaoStockError(f"{fn.__name__}: {rs.error_code} {rs.error_msg}")
                    rows = []
                    while (rs.error_code == "0") & rs.next():
                        rows.append(rs.get_row_data())
                    if rs.error_code != "0":
                        raise BaoStockError(f"{fn.__name__}: {rs.error_code} {rs.error_msg}")
                return list(rs.fields), rows
            except Exception as e:  # noqa: BLE001  网络错误、未登录、超时都重试
                err = e
                log.debug("BaoStock %s 第 %d 次失败：%s", fn.__name__, attempt + 1, e)
                time.sleep(2 * (attempt + 1))
                self.close()
                with contextlib.suppress(Exception):
                    self._login()
        raise BaoStockError(f"{fn.__name__}{args} 多次重试仍失败：{err}")

    # ---------- 基本信息 ----------
    def stock_basic(self) -> list[dict]:
        """全部证券的基本资料：code, code_name, ipoDate, outDate, type, status。"""
        cols, rows = self._query(bs.query_stock_basic)
        return [dict(zip(cols, r)) for r in rows]

    def trade_dates(self, start: str, end: str) -> list[str]:
        cols, rows = self._query(bs.query_trade_dates, start_date=start, end_date=end)
        i_d, i_t = cols.index("calendar_date"), cols.index("is_trading_day")
        return [r[i_d] for r in rows if r[i_t] == "1"]

    # ---------- 单只股票完整历史 ----------
    def history(self, bs_code: str, end: str) -> list[Bar]:
        cols, rows = self._query(
            bs.query_history_k_data_plus,
            bs_code,
            # 计算只用到最低价、收盘价、前收盘价；不取开盘价和最高价，下载量少约四分之一
            "date,low,close,preclose,volume,tradestatus",
            start_date=HIST_START,
            end_date=end,
            frequency="d",
            adjustflag="3",  # 不复权
        )
        return [_bar(dict(zip(cols, r))) for r in rows]

    def adjust_factors(self, bs_code: str, end: str) -> list[tuple[str, float]]:
        """[(除权除息日, 后复权因子)]，按日期升序。"""
        cols, rows = self._query(bs.query_adjust_factor, code=bs_code, start_date=HIST_START, end_date=end)
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            v = _f(d.get("backAdjustFactor"))
            if d.get("dividOperateDate") and v > 0:
                out.append((d["dividOperateDate"], v))
        out.sort()
        return out

    def history_with_adj(self, bs_code: str, end: str) -> tuple[list[Bar], list[float]]:
        """历史日线 + 每根 K 线对应的后复权因子（因子在除权除息日当天生效）。"""
        bars = self.history(bs_code, end)
        events = self.adjust_factors(bs_code, end)
        adjs, j, cur = [], 0, 1.0  # 第一次除权之前因子为 1
        for b in bars:
            while j < len(events) and events[j][0] <= b.date:
                cur = events[j][1]
                j += 1
            adjs.append(cur)
        return bars, adjs

    def latest_date(self, probe_code: str = "sh.600000") -> str:
        """BaoStock 已入库的最新交易日。"""
        today = bj_today()
        start = add_days(today, -40)
        cols, rows = self._query(
            bs.query_history_k_data_plus, probe_code, "date", start_date=start, end_date=today,
            frequency="d", adjustflag="3",
        )
        if not rows:
            raise BaoStockError("取不到最新交易日")
        return rows[-1][0]

    # ---------- 某一天的全市场数据 ----------
    def daily_bars(self, date: str) -> dict[str, tuple]:
        """{6 位代码: (市场, 日线, 是否 ST, (市盈率TTM, 市净率MRQ, 市销率TTM))}；BaoStock 还没入库时返回空。"""
        cols, rows = self._query(bs.query_daily_history_k_AStock, date=date)
        out = {}
        for r in rows:
            d = dict(zip(cols, r))
            if d.get("date") and d["date"] != date:
                continue
            mkt, code = d["code"].split(".")
            val = (_f(d.get("peTTM")), _f(d.get("pbMRQ")), _f(d.get("psTTM")))
            out[code] = (mkt, _bar(d), d.get("isST") == "1", val)
        return out

    def daily_factors(self, date: str) -> dict[str, float]:
        """{6 位代码: 截至该日的后复权因子}。只取除权除息日不晚于 date 的记录。"""
        cols, rows = self._query(bs.query_daily_adjust_factor, date=date)
        best: dict[str, tuple[str, float]] = {}
        for r in rows:
            d = dict(zip(cols, r))
            dd, v = d.get("dividOperateDate", ""), _f(d.get("backAdjustFactor"))
            if not dd or dd > date or not v > 0:
                continue
            code = d["code"].split(".")[-1]
            if code not in best or dd >= best[code][0]:
                best[code] = (dd, v)
        return {c: v for c, (dd, v) in best.items()}


def _bar(d: dict) -> Bar:
    vol = _f(d.get("volume"))
    traded = d.get("tradestatus", "1") != "0" and not (vol == 0)
    return Bar(
        date=d["date"],
        open=_f(d.get("open")),
        high=_f(d.get("high")),
        low=_f(d.get("low")),
        close=_f(d.get("close")),
        preclose=_f(d.get("preclose")),
        traded=traded,
    )


# ---------- 多进程全量构建 ----------
# BaoStock 一个进程只有一个连接，多开几个进程并行下载以缩短首次构建时间。
_worker_api: BaoStock | None = None


def _worker_init() -> None:
    # 注意：进程池的初始化函数抛异常会导致子进程被无限重启，所以这里不能抛异常
    global _worker_api
    logging.basicConfig(level=logging.WARNING)
    try:
        _worker_api = BaoStock()
    except Exception:  # noqa: BLE001
        _worker_api = None


def worker_fetch(args: tuple[str, str, str, str]):
    """在子进程里运行。
    kind = history：返回 (代码, bars, adjs, 错误信息)
    kind = factors：返回 (代码, [(除权日, 后复权因子)], None, 错误信息)
    """
    global _worker_api
    code, mkt, end, kind = args
    try:
        if _worker_api is None:
            _worker_api = BaoStock()
        if kind == "factors":
            return code, _worker_api.adjust_factors(to_bs_code(code, mkt), end), None, ""
        bars, adjs = _worker_api.history_with_adj(to_bs_code(code, mkt), end)
        return code, bars, adjs, ""
    except Exception as e:  # noqa: BLE001
        return code, None, None, str(e)
