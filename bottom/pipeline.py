"""更新流程：全量构建（首次 / 每月） + 每周核对 + 每日增量。"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import multiprocessing as mp
import os
import time
from collections import Counter
from typing import Iterator

import math
import statistics

from .engine import Stock, apply_bar, fill_issue_price, fin_period, is_a_share, is_pos, rebuild_from_history
from .util import add_days, bj_now, bj_today, summary

log = logging.getLogger(__name__)

MAX_CATCHUP_DAYS = 15  # 落下超过这么多个交易日就直接全量重建
CHECKPOINT_EVERY = 300  # 全量构建时每处理这么多只保存一次进度
EVENT_RATIO_LIMIT = 0.15  # 某天被识别为除权除息的比例超过它，说明数据对不上，放弃当天更新
EVENT_CHECK_MIN = 50  # 当天有交易的股票少于这个数时不做上面的检查
TENCENT_PE_FIELDS = (39, 52, 53)  # 腾讯行情里可能是市盈率的字段位置，运行时和 BaoStock 核对后选用
TENCENT_PB_FIELDS = (46,)
VALUATION_MATCH_TOL = 0.03  # 腾讯与 BaoStock 估值的中位相对误差小于它，才认为字段含义一致
FIN_REFRESH_DAYS = 7  # 财务数据（业绩报表）每隔几天重新取一次，补上新股和更正
FIN_MIN_COVERAGE = 0.5  # 业绩报表覆盖的股票少于上市股票的一半，视为数据不完整
FIN_PB_TOL = 0.10  # 用 收盘价/每股净资产 和 BaoStock 市净率核对，中位相对误差的上限
COMPLETE_RATIO = 0.95  # BaoStock 全市场单日数据条数达到上市股票数的这个比例，才算当天已入库完整（停牌股也有记录）
F10_REFRESH_DAYS = 7  # 东方财富发行资料每周刷新一次
F10_MIN_RATIO, F10_MAX_RATIO = 0.05, 3.0  # 发行价 / 上市首日收盘价 的合理范围
FACTOR_REL_TOL = 2e-4  # 每周核对时，复权因子相对误差超过它就重算


def set_valuation(s: Stock, val, date: str) -> bool:
    """写入一只股票的估值。三项都缺（如停牌）时保留旧值。"""
    pe, pb, ps = (float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v != 0 else math.nan for v in val)
    if all(math.isnan(v) for v in (pe, pb, ps)):
        return False
    s.pe, s.pb, s.ps, s.val_date = pe, pb, ps, date
    return True


class Sources:
    """真实数据源。测试时用假的子类替换。"""

    def __init__(self):
        from .src_baostock import BaoStock

        self.bs = BaoStock()
        self.workers = int(os.environ.get("BS_WORKERS", "4"))

    # BaoStock（沪深）
    def stock_basic(self):
        return self.bs.stock_basic()

    def trade_dates(self, start, end):
        return self.bs.trade_dates(start, end)

    def latest_date(self):
        return self.bs.latest_date()

    def daily_bars(self, date):
        return self.bs.daily_bars(date)

    def daily_factors(self, date):
        return self.bs.daily_factors(date)

    def history_shsz(self, code, mkt, end):
        from .src_baostock import to_bs_code

        return self.bs.history_with_adj(to_bs_code(code, mkt), end)

    def factors_shsz(self, code, mkt, end):
        from .src_baostock import to_bs_code

        return self.bs.adjust_factors(to_bs_code(code, mkt), end)

    def fetch_shsz(self, items: list[tuple[str, str]], end: str, kind: str = "history") -> Iterator[tuple]:
        """并行下载多只沪深股票，逐个产出 (代码, 数据, adjs, 错误)。
        kind = history：完整历史日线 + 复权因子；kind = factors：只取复权因子（每周核对用）。"""
        from .src_baostock import _worker_init, worker_fetch

        remaining = dict(items)
        if self.workers > 1 and len(items) >= 20:
            n = err = 0
            ctx = mp.get_context("spawn")
            with ctx.Pool(self.workers, initializer=_worker_init) as pool:
                for code, bars, adjs, e in pool.imap_unordered(
                    worker_fetch, [(c, m, end, kind) for c, m in items], chunksize=4
                ):
                    n += 1
                    if e:
                        err += 1
                    else:
                        remaining.pop(code, None)
                        yield code, bars, adjs, ""
                    if n >= 40 and err > n / 2:
                        log.warning("并行下载失败率过高（%d/%d），改为单进程下载", err, n)
                        break
        # 单进程：worker 数为 1、数量很少、或并行失败后的剩余部分
        for code, mkt in list(remaining.items()):
            try:
                if kind == "factors":
                    yield code, self.factors_shsz(code, mkt, end), None, ""
                else:
                    bars, adjs = self.history_shsz(code, mkt, end)
                    yield code, bars, adjs, ""
            except Exception as e:  # noqa: BLE001
                yield code, None, None, str(e)

    # 北交所 / 腾讯 / 东方财富
    def bj_stock_list(self):
        from .src_other import bj_stock_list

        return bj_stock_list()

    def bj_history(self, code, list_date):
        from .src_other import bj_history

        return bj_history(code, list_date)

    def quotes(self, syms):
        from .src_other import tencent_quotes

        return tencent_quotes(syms)

    def issue_prices(self):
        from .src_other import issue_prices

        return issue_prices()

    def issue_info_f10(self):
        from .src_other import issue_info_f10

        return issue_info_f10()

    def financials(self, period):
        from .src_other import financials

        return financials(period)

    def close(self):
        self.bs.close()


class Updater:
    def __init__(self, src, stocks: dict[str, Stock], meta: dict, today: str | None = None,
                 deadline: float | None = None, now_hm: str | None = None):
        self.src = src
        self.stocks = stocks
        self.meta = meta
        self.today = today or bj_today()
        self.now_hm = now_hm or bj_now().strftime("%H:%M")
        self.deadline = deadline  # time.time() 时间戳；超时就保存进度退出，下次接着做
        self.save_cb = None  # 由调用方设置：保存进度的回调
        self.notes: list[str] = []

    # ---------- 工具 ----------
    def note(self, msg: str) -> None:
        log.info(msg)
        self.notes.append(msg)

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline

    def checkpoint(self) -> None:
        if self.save_cb:
            self.save_cb(self.stocks, self.meta)

    # ---------- 股票列表 ----------
    def refresh_universe(self) -> None:
        info: dict[str, dict] = {}
        for r in self.src.stock_basic():
            if r.get("type") != "1" or r.get("status") != "1":
                continue
            mkt, code = r["code"].split(".")
            ld = r.get("ipoDate", "")
            if not is_a_share(code, mkt) or (ld and ld > self.today):
                continue
            info[code] = {"mkt": mkt, "name": r.get("code_name", ""), "list_date": ld}
        n_shsz = len(info)
        bj_ok = True
        try:
            for r in self.src.bj_stock_list():
                if r["list_date"] and r["list_date"] > self.today:
                    continue
                info[r["code"]] = {"mkt": "bj", "name": r["name"], "list_date": r["list_date"]}
        except Exception as e:  # noqa: BLE001
            bj_ok = False
            self.note(f"⚠️ 北交所股票列表获取失败，本次沿用旧列表：{e}")

        # 列表明显不全（比上次少一成以上）时，认为是数据源出错，这次不删除任何股票
        n_bj = len(info) - n_shsz
        old_shsz = sum(1 for s in self.stocks.values() if s.mkt != "bj")
        old_bj = sum(1 for s in self.stocks.values() if s.mkt == "bj")
        can_remove = {"shsz": n_shsz >= 0.9 * old_shsz, "bj": bj_ok and n_bj >= 0.9 * old_bj}
        removed = []
        for code in list(self.stocks):
            if code in info:
                continue
            if can_remove["bj" if self.stocks[code].mkt == "bj" else "shsz"]:
                removed.append(f"{code}{self.stocks[code].name}")
                del self.stocks[code]  # 已退市
        if removed:
            self.note(f"剔除已退市 {len(removed)} 只：{'、'.join(removed[:20])}")
        added = []
        for code, d in info.items():
            s = self.stocks.get(code)
            if s is None:
                s = self.stocks[code] = Stock(code=code, mkt=d["mkt"])
                added.append(f"{code}{d['name']}")
            s.mkt = d["mkt"]
            s.name = d["name"] or s.name
            s.list_date = d["list_date"] or s.list_date
            s.is_st = "ST" in s.name.upper()
        self.note(f"股票列表：沪深 {n_shsz} 只，北交所 {len(info) - n_shsz} 只")
        if added and len(added) < 200:  # 首次构建时全是新增，不逐个列出
            self.note(f"新上市 {len(added)} 只：{'、'.join(added[:20])}")

    # ---------- 全量构建 ----------
    def complete_date(self) -> str:
        """BaoStock 已经完整入库的最新交易日。

        BaoStock 每天傍晚陆续入库，个别股票已有当天数据不代表全部都有；
        用全市场单日数据的条数核对，不完整就退回上一个交易日。"""
        d = self.src.latest_date()
        listed = sum(1 for s in self.stocks.values() if s.mkt != "bj" and (not s.list_date or s.list_date <= d))
        n = len(self.src.daily_bars(d))
        if n >= COMPLETE_RATIO * listed:
            return d
        prev = max([x for x in self.src.trade_dates(add_days(d, -20), d) if x < d], default=d)
        self.note(f"BaoStock {d} 的数据还没入库完整（{n}/{listed}），全量构建截止到 {prev}")
        return prev

    def start_full_build(self) -> None:
        self.meta["build_pending"] = True
        self.meta["build_gen"] = self.today
        self.meta["build_end"] = self.complete_date()
        self.meta["build_started"] = time.time()
        self.note(f"开始全量构建，数据截止 {self.meta['build_end']}")

    def run_full_build(self) -> bool:
        """返回 True 表示全部完成。中途超时会保存进度，下次运行接着做。"""
        gen, end = self.meta["build_gen"], self.meta["build_end"]
        todo = [s for s in self.stocks.values() if s.built < gen]
        shsz = [(s.code, s.mkt) for s in todo if s.mkt != "bj"]
        bj = [s.code for s in todo if s.mkt == "bj"]
        self.note(f"全量构建：待处理沪深 {len(shsz)} 只、北交所 {len(bj)} 只")
        done, failed = self._rebuild_shsz(shsz, end, gen)
        if self.out_of_time():
            self.note(f"⏸ 时间用完，已完成 {done} 只，下次运行继续")
            return False
        done_bj, failed_bj = self._rebuild_bj(bj, gen)
        if self.out_of_time():
            self.note("⏸ 时间用完，下次运行继续")
            return False
        self.update_issue_prices(force=True)
        self.meta["build_pending"] = False
        self.meta["bs_date"] = end
        self.meta["last_full_build"] = gen
        mins = (time.time() - self.meta.get("build_started", time.time())) / 60
        self.note(f"全量构建完成：沪深 {done} 只（失败 {len(failed)}），北交所 {done_bj} 只（失败 {len(failed_bj)}）"
                  + (f"，从开始到完成共 {mins / 60:.1f} 小时（含中间等待）" if mins > 1 else ""))
        self.meta.pop("build_started", None)
        if failed or failed_bj:
            self.note("失败的股票保留旧数据，下次重建时再试：" + ",".join((failed + failed_bj)[:30]))
        return True

    def _rebuild_shsz(self, items, end, gen) -> tuple[int, list[str]]:
        done, failed, started = 0, [], time.time()
        gen_iter = self.src.fetch_shsz(items, end)
        try:
            for code, bars, adjs, err in gen_iter:
                if err or bars is None:
                    failed.append(code)
                    continue
                self._store_rebuilt(code, bars, adjs, gen, end)
                done += 1
                if done % CHECKPOINT_EVERY == 0:
                    rate = done / max(time.time() - started, 1)
                    log.info("已完成 %d/%d（%.1f 只/秒）", done, len(items), rate)
                    self.checkpoint()
                if self.out_of_time():
                    break
        finally:
            close = getattr(gen_iter, "close", None)
            if close:
                close()
        # 失败的在主进程里再试一次
        retry, failed = failed, []
        for code in retry:
            if self.out_of_time():
                failed.append(code)
                continue
            try:
                bars, adjs = self.src.history_shsz(code, self.stocks[code].mkt, end)
                self._store_rebuilt(code, bars, adjs, gen, end)
                done += 1
            except Exception as e:  # noqa: BLE001
                log.warning("%s 下载失败：%s", code, e)
                failed.append(code)
        return done, failed

    def _store_rebuilt(self, code, bars, adjs, gen, end) -> None:
        prev = self.stocks.get(code)
        if prev is None:
            return
        if not bars:
            # BaoStock 还没有这只股票的数据（如刚上市）：标记已处理，等每日更新时再补
            prev.built = gen
            return
        # 注意：不把 last_seen 强行设为 end。如果 BaoStock 当时还没入库这只股票最新一天的数据，
        # 它的 last_seen 会早于 end，之后的每日更新会把它当作“没跟上进度”重新用完整历史计算
        self.stocks[code] = rebuild_from_history(prev, bars, adjs, built=gen)

    def _rebuild_bj(self, codes, gen) -> tuple[int, list[str]]:
        done, failed = 0, []
        for code in codes:
            if self.out_of_time():
                break
            s = self.stocks[code]
            try:
                bars, adjs = self.src.bj_history(code, s.list_date)
                if bars:
                    self.stocks[code] = rebuild_from_history(s, bars, adjs, built=gen)
                else:
                    s.built = gen
                done += 1
            except Exception as e:  # noqa: BLE001
                log.warning("北交所 %s 下载失败：%s", code, e)
                failed.append(code)
            time.sleep(0.2)
        return done, failed

    # ---------- 每周核对 ----------
    def run_verify(self) -> None:
        """每周核对：不重新下载全部历史，只核对每只沪深股票的复权因子。

        后复权价一旦算出就不会再变，所以只要“当前复权因子”和 BaoStock 一致，
        就说明这期间没有漏掉或算错除权除息；对不上的股票才用完整历史重算。
        北交所数据量小，直接全部重算。每月第一个周六另做一次完整重建，兜底数据源事后修正历史的情况。"""
        started = time.time()
        end = self.meta.get("bs_date")
        items = [s for s in self.stocks.values() if s.mkt != "bj" and s.has_data() and s.last_seen == end]
        bad, failed, checked = [], [], 0
        gen_iter = self.src.fetch_shsz([(s.code, s.mkt) for s in items], end, kind="factors")
        try:
            for code, events, _, err in gen_iter:
                if err or events is None:
                    failed.append(code)
                    continue
                checked += 1
                s = self.stocks[code]
                expected = 1.0
                for d, v in events:
                    if d <= s.last_seen:
                        expected = v
                if abs(expected / s.adj - 1) > FACTOR_REL_TOL:
                    bad.append(code)
                    log.warning("%s 复权因子不一致：本地 %.6f，BaoStock %.6f", code, s.adj, expected)
                if self.out_of_time():
                    break
        finally:
            close = getattr(gen_iter, "close", None)
            if close:
                close()
        self.note(f"每周核对：沪深核对 {checked} 只，复权因子不一致 {len(bad)} 只，查询失败 {len(failed)} 只")
        if bad:
            done, f2 = self._rebuild_shsz([(c, self.stocks[c].mkt) for c in bad], end, self.today)
            self.note(f"每周核对：重算不一致的股票 {done} 只：{'、'.join(bad[:20])}")
        bj = [s.code for s in self.stocks.values() if s.mkt == "bj"]
        done_bj, failed_bj = self._rebuild_bj(bj, self.today)
        self.note(f"每周核对：北交所重算 {done_bj} 只（失败 {len(failed_bj)}），用时 {(time.time() - started) / 60:.0f} 分钟")

    # ---------- 每日增量（沪深，BaoStock） ----------
    def daily_shsz(self) -> None:
        last = self.meta.get("bs_date")
        dates = self.src.trade_dates(add_days(last, 1), self.today) if last < self.today else []
        if not dates:
            self.note(f"沪深：没有新的交易日（已处理到 {last}）")
        if len(dates) > MAX_CATCHUP_DAYS:
            self.note(f"沪深：落下 {len(dates)} 个交易日，改为全量构建")
            self.start_full_build()
            return
        # 没跟上进度的股票（例如全量构建时下载失败、刚上市还没数据）：不能直接增量，改用完整历史重算
        need_rebuild: set[str] = {
            s.code for s in self.stocks.values()
            if s.mkt != "bj" and s.built and s.last_seen < last and (not s.list_date or s.list_date <= last)
        }
        for d in dates:
            listed = [s for s in self.stocks.values() if s.mkt != "bj" and s.built and (not s.list_date or s.list_date <= d)]
            bars = self.src.daily_bars(d)
            if len(bars) < COMPLETE_RATIO * len(listed):
                self.note(f"沪深：BaoStock 还没有 {d} 的完整数据（{len(bars)} 条），下次运行再更新")
                break
            try:
                factors = self.src.daily_factors(d)
            except Exception as e:  # noqa: BLE001
                factors = {}
                self.note(f"⚠️ {d} 复权因子获取失败，改用前收盘价识别除权：{e}")
            backup = {s.code: copy.copy(s) for s in listed}
            stat = Counter()
            for s in listed:
                item = bars.get(s.code)
                if item is None:
                    # 当天没有这只股票的数据：不猜，之后用完整历史重算它
                    if s.last_seen < d:
                        need_rebuild.add(s.code)
                    continue
                if s.code in need_rebuild:
                    continue
                r = apply_bar(s, item[1], factors.get(s.code))
                if len(item) > 3:
                    set_valuation(s, item[3], d)
                stat[r] += 1
                if r == "event" and s.code not in factors:
                    stat["event_by_preclose"] += 1
            traded = stat["ok"] + stat["event"]
            if traded >= EVENT_CHECK_MIN and stat["event_by_preclose"] > EVENT_RATIO_LIMIT * traded:
                for code, old in backup.items():
                    self.stocks[code] = old
                self.note(f"❌ {d}：{stat['event_by_preclose']} 只股票前收盘价对不上，数据可能有误，已放弃当天更新")
                break
            self.meta["bs_date"] = d
            self.note(f"沪深 {d}：正常 {stat['ok']}，除权除息 {stat['event']}，停牌 {stat['suspended']}，缺数据 {len(need_rebuild)}")
        # 新上市、缺数据的股票：用完整历史重算
        end = self.meta.get("bs_date")
        new = {s.code for s in self.stocks.values() if s.mkt != "bj" and not s.built}
        todo = sorted(need_rebuild | new)
        if todo:
            done, failed = self._rebuild_shsz([(c, self.stocks[c].mkt) for c in todo], end, self.today)
            self.note(f"沪深：单独重算 {done} 只（新股/缺数据），失败 {len(failed)}")

    # ---------- 每日增量（北交所，腾讯） ----------
    def daily_bj(self) -> None:
        bj = [s for s in self.stocks.values() if s.mkt == "bj"]
        if not bj:
            return
        recent = self.src.trade_dates(add_days(self.today, -20), self.today)
        prev_td = max([d for d in recent if d < self.today], default="")
        is_td = self.today in recent
        stale = [s.code for s in bj if not s.built or s.last_seen < prev_td]
        if stale:
            done, failed = self._rebuild_bj(stale, self.today)
            self.note(f"北交所：用完整历史重算 {done} 只，失败 {len(failed)}")
        if not is_td or self.now_hm < "15:05":
            return
        quotes = self.src.quotes(["bj" + s.code for s in bj])
        backup = {s.code: copy.copy(s) for s in bj}
        stat = Counter()
        for s in bj:
            q = quotes.get("bj" + s.code)
            if q is None or q.date != self.today or not s.built:
                continue
            stat[apply_bar(s, q.to_bar())] += 1
        traded = stat["ok"] + stat["event"]
        if traded >= EVENT_CHECK_MIN and stat["event"] > EVENT_RATIO_LIMIT * traded:
            for code, old in backup.items():
                self.stocks[code] = old
            self.note(f"❌ 北交所 {self.today}：{stat['event']} 只前收盘价对不上，已放弃当天更新")
            return
        self.note(f"北交所 {self.today}：正常 {stat['ok']}，除权除息 {stat['event']}，停牌 {stat['suspended']}")

    # ---------- 估值（价值分用） ----------
    def refresh_valuation_shsz(self) -> None:
        """沪深估值来自 BaoStock 全市场单日数据。每日增量时已顺带写入；
        这里只在大量股票缺最新估值时（首次上线、完整重建之后）补取一次。"""
        end = self.meta.get("bs_date")
        shsz = [s for s in self.stocks.values() if s.mkt != "bj" and s.has_data()]
        stale = [s for s in shsz if s.val_date != end]
        if not end or not shsz or len(stale) < 0.05 * len(shsz):
            return
        bars = self.src.daily_bars(end)
        n = 0
        for s in stale:
            item = bars.get(s.code)
            if item is not None and len(item) > 3 and set_valuation(s, item[3], end):
                n += 1
        self.note(f"估值：补充沪深 {n} 只（{end}）")

    def refresh_bj_valuation(self) -> None:
        """北交所估值来自腾讯行情。腾讯的字段没有官方文档，先拿几十只沪深股票
        和 BaoStock 的市盈率、市净率对一下，确认字段含义一致才使用；对不上就不给北交所打分。"""
        bj = [s for s in self.stocks.values() if s.mkt == "bj" and s.has_data()]
        end = self.meta.get("bs_date")
        sample = [s for s in self.stocks.values()
                  if s.mkt != "bj" and s.val_date == end and is_pos(s.pe) and is_pos(s.pb)][:40]
        if not bj or len(sample) < 10:
            return
        quotes = self.src.quotes([s.mkt + s.code for s in sample] + ["bj" + s.code for s in bj])

        def best(candidates, attr):
            choice = None
            for i in candidates:
                diffs = []
                for s in sample:
                    q = quotes.get(s.mkt + s.code)
                    v = q.field(i) if q is not None else math.nan
                    if is_pos(v):
                        diffs.append(abs(v / getattr(s, attr) - 1))
                if len(diffs) >= 10:
                    med = statistics.median(diffs)
                    if med < VALUATION_MATCH_TOL and (choice is None or med < choice[1]):
                        choice = (i, med)
            return choice

        pe_f, pb_f = best(TENCENT_PE_FIELDS, "pe"), best(TENCENT_PB_FIELDS, "pb")
        if pe_f is None or pb_f is None:
            self.note("⚠️ 北交所估值：腾讯行情字段和 BaoStock 对不上，本次不更新北交所价值分")
            return
        n = 0
        for s in bj:
            q = quotes.get("bj" + s.code)
            if q is not None and q.date and set_valuation(s, (q.field(pe_f[0]), q.field(pb_f[0]), math.nan), q.date):
                n += 1
        self.note(f"北交所估值：腾讯字段核对通过（市盈率误差 {pe_f[1]:.1%}，市净率误差 {pb_f[1]:.1%}），更新 {n} 只")

    # ---------- 财务数据（质量分、成长分、行业） ----------
    def refresh_financials(self) -> None:
        """全市场用同一个报告期（都已过披露截止日的最近一期）比较，保证公平。
        每周刷新一次；进入新的报告期时立即刷新。数据先核对再使用，核对不过就保留旧数据。"""
        period = fin_period(self.today)
        last = self.meta.get("fin_fetched", "")
        if self.meta.get("fin_period") == period and last and add_days(last, FIN_REFRESH_DAYS) > self.today:
            return
        rows = self.src.financials(period)
        ok, msg = self.check_financials(rows)
        if not ok:
            self.note(f"⚠️ 财务数据（{period}）没有通过核对，本次不更新质量分、成长分：{msg}")
            return
        n = 0
        for r in rows:
            s = self.stocks.get(r["code"])
            if s is None:
                continue
            for k in ("bps", "roe", "gpm", "ocfps", "rev_yoy", "np_yoy"):
                setattr(s, k, r[k])
            if r["industry"]:
                s.industry = r["industry"]
            s.fin_period = period
            n += 1
        # 这一期没有报告的股票（如刚上市）：清掉旧报告期的数据，避免和别人用不同报告期比较
        for s in self.stocks.values():
            if s.fin_period and s.fin_period != period:
                for k in ("bps", "roe", "gpm", "ocfps", "rev_yoy", "np_yoy"):
                    setattr(s, k, math.nan)
                s.fin_period = ""
        self.meta["fin_period"] = period
        self.meta["fin_fetched"] = self.today
        self.note(f"财务数据：{period} 报告期，匹配 {n} 只；{msg}")

    def check_financials(self, rows: list[dict]) -> tuple[bool, str]:
        """核对东方财富业绩报表的字段含义是否正确（防止数据源改格式后错位）。"""
        listed = sum(1 for s in self.stocks.values() if s.has_data())
        matched = [r for r in rows if r["code"] in self.stocks]
        if listed and len(matched) < FIN_MIN_COVERAGE * listed:
            return False, f"只覆盖 {len(matched)}/{listed} 只"
        roe = [r["roe"] for r in matched if math.isfinite(r["roe"])]
        gpm = [r["gpm"] for r in matched if math.isfinite(r["gpm"])]
        if len(roe) < 0.5 * len(matched) or not -20 < statistics.median(roe) < 40:
            return False, "净资产收益率数值异常"
        if gpm and not 3 < statistics.median(gpm) < 80:
            return False, "毛利率数值异常"
        # 收盘价 / 每股净资产 应该约等于 BaoStock 的市净率
        diffs = []
        for r in matched:
            s = self.stocks[r["code"]]
            if is_pos(r["bps"]) and is_pos(s.pb) and is_pos(s.last_close) and s.val_date == s.last_trade:
                diffs.append(abs(s.last_close / r["bps"] / s.pb - 1))
        if len(diffs) >= 30:
            med = statistics.median(diffs)
            if med > FIN_PB_TOL:
                return False, f"每股净资产和 BaoStock 市净率对不上（中位误差 {med:.1%}）"
            return True, f"每股净资产与 BaoStock 市净率核对通过（中位误差 {med:.1%}）"
        return True, "暂无足够的市净率数据做交叉核对，只做了数值范围检查"

    # ---------- 发行价 ----------
    def update_issue_prices(self, force: bool = False) -> None:
        """发行价来源优先级：东方财富新股数据（em）> 东方财富 F10 发行资料（f10）> 上市首日前收盘价推断（infer）。

        换股吸收合并等方式上市的股票没有真正的发行价（issue_src = none），不计入历史最低。"""
        recent = add_days(self.today, -60)
        missing = [s for s in self.stocks.values() if s.issue_src != "em" and s.list_date >= recent]
        if force or missing:
            prices = self.src.issue_prices()
            n = 0
            for code, p in prices.items():
                s = self.stocks.get(code)
                if s is not None:
                    s.issue_price, s.issue_src = p, "em"
                    n += 1
            if force:
                self.note(f"发行价：东方财富新股数据匹配 {n} 只")
        last = self.meta.get("f10_issue_date", "")
        pending = [s for s in self.stocks.values() if s.issue_src in ("", "infer") and s.list_date >= recent]
        due = force or not last or last <= add_days(self.today, -F10_REFRESH_DAYS) or (pending and last != self.today)
        if due:
            try:
                info = self.src.issue_info_f10()
            except Exception as e:  # noqa: BLE001  只影响老股票发行价的来源，沿用原值
                self.note(f"⚠️ 东方财富发行资料获取失败，发行价沿用原值：{type(e).__name__}: {e}")
                info = None
            if info is not None and self._apply_f10(info):
                self.meta["f10_issue_date"] = self.today
        for s in self.stocks.values():
            fill_issue_price(s)

    def _apply_f10(self, info: dict[str, dict]) -> bool:
        if len(info) < 0.8 * len(self.stocks):
            self.note(f"⚠️ 东方财富发行资料只有 {len(info)} 条（股票 {len(self.stocks)} 只），数据不完整，本次不使用")
            return False
        used = agree = bad = 0
        diffs, swaps = [], []
        for code, rec in info.items():
            s = self.stocks.get(code)
            if s is None or s.issue_src == "em":
                continue
            if rec.get("swap"):
                s.issue_price, s.issue_src = math.nan, "none"
                swaps.append(f"{s.code}{s.name}")
                continue
            p, fc = rec.get("price"), rec.get("first_close")
            if not is_pos(p):
                continue
            ld = rec.get("list_date", "")
            if ld and s.list_date and abs((dt.date.fromisoformat(ld) - dt.date.fromisoformat(s.list_date)).days) > 30:
                bad += 1  # 上市日期对不上（代码可能被重复使用过）
                continue
            ref = fc if is_pos(fc) else s.ipo_preclose
            if is_pos(ref) and not (F10_MIN_RATIO <= p / ref <= F10_MAX_RATIO):
                bad += 1
                continue
            used += 1
            if is_pos(s.ipo_preclose):
                if abs(p - s.ipo_preclose) < 0.011:
                    agree += 1
                else:
                    diffs.append(f"{s.code}{s.name} 发行资料 {p:g} / 首日前收盘 {s.ipo_preclose:g}")
            s.issue_price, s.issue_src = p, "f10"
        self.note(f"发行价（东方财富发行资料）：采用 {used} 只，其中和上市首日前收盘价一致 {agree} 只、不一致 {len(diffs)} 只；"
                  f"换股/合并上市没有发行价 {len(swaps)} 只；数值或上市日期对不上、未采用 {bad} 只")
        if diffs:
            self.note("　不一致的（以发行资料为准）：" + "；".join(diffs[:10]) + (" 等" if len(diffs) > 10 else ""))
        if swaps:
            self.note("　换股/合并上市：" + "、".join(swaps[:10]) + (" 等" if len(swaps) > 10 else ""))
        return True

    # ---------- 总入口 ----------
    def run(self, mode: str) -> None:
        self.refresh_universe()
        if mode == "rebuild" and not self.meta.get("build_pending"):
            self.start_full_build()
        elif not self.meta.get("bs_date") and not self.meta.get("build_pending"):
            self.note("没有历史状态，先做一次全量构建")
            self.start_full_build()
        if self.meta.get("build_pending"):
            if not self.run_full_build():
                return
        self.daily_shsz()
        if self.meta.get("build_pending"):  # 落下太多天，触发了全量构建
            if not self.run_full_build():
                return
            self.daily_shsz()
        if mode == "verify":
            self.run_verify()
        self.daily_bj()
        for step in (self.refresh_valuation_shsz, self.refresh_bj_valuation, self.refresh_financials):
            try:
                step()
            except Exception as e:  # noqa: BLE001  估值失败只影响价值分，不影响主数据
                self.note(f"⚠️ {step.__name__} 失败（只影响评分，不影响历史最低价数据）：{type(e).__name__}: {e}")
        self.update_issue_prices()
        self.meta["updated_at"] = bj_now().strftime("%Y-%m-%d %H:%M")


def report(up: Updater) -> None:
    stocks = up.stocks.values()
    with_data = [s for s in stocks if s.has_data()]
    both = [s for s in stocks if s.issue_src == "em" and s.ipo_preclose == s.ipo_preclose and s.ipo_preclose > 0]
    agree = sum(1 for s in both if abs(s.issue_price - s.ipo_preclose) < 0.011)
    summary("## A股历史最低价比值 · 本次运行")
    summary("")
    for n in up.notes:
        summary(f"- {n}")
    summary(f"- 有数据的股票：{len(with_data)} / {len(up.stocks)}")
    summary(f"- 发行价：东方财富新股数据 {sum(1 for s in stocks if s.issue_src == 'em')}，"
            f"东方财富发行资料 {sum(1 for s in stocks if s.issue_src == 'f10')}，"
            f"首日前收盘推断 {sum(1 for s in stocks if s.issue_src == 'infer')}，"
            f"换股/合并上市无发行价 {sum(1 for s in stocks if s.issue_src == 'none')}，"
            f"缺失 {sum(1 for s in stocks if not s.issue_src)}")
    if both:
        summary(f"- 校验：东方财富发行价与首日前收盘价一致 {agree}/{len(both)}")
    from .engine import compute_scores

    sc = compute_scores(stocks)
    cnt = {k: sum(1 for d in sc.values() if k in d) for k in ("value", "quality", "growth", "total")}
    summary(f"- 评分：价值分 {cnt['value']} 只，质量分 {cnt['quality']} 只，成长分 {cnt['growth']} 只，"
            f"综合分 {cnt['total']} 只；有行业信息 {sum(1 for s in stocks if s.industry)} 只")
