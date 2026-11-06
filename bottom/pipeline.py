"""更新流程：全量构建（首次 / 每周） + 每日增量。"""

from __future__ import annotations

import copy
import logging
import multiprocessing as mp
import os
import time
from collections import Counter
from typing import Iterator

from .engine import Stock, apply_bar, fill_issue_price, is_a_share, rebuild_from_history
from .util import add_days, bj_now, bj_today, summary

log = logging.getLogger(__name__)

MAX_CATCHUP_DAYS = 15  # 落下超过这么多个交易日就直接全量重建
CHECKPOINT_EVERY = 300  # 全量构建时每处理这么多只保存一次进度
EVENT_RATIO_LIMIT = 0.15  # 某天被识别为除权除息的比例超过它，说明数据对不上，放弃当天更新
EVENT_CHECK_MIN = 50  # 当天有交易的股票少于这个数时不做上面的检查


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

    def fetch_shsz(self, items: list[tuple[str, str]], end: str) -> Iterator[tuple]:
        """并行下载多只沪深股票的完整历史，逐个产出 (代码, bars, adjs, 错误)。"""
        from .src_baostock import _worker_init, worker_fetch

        remaining = dict(items)
        if self.workers > 1 and len(items) >= 20:
            n = err = 0
            ctx = mp.get_context("spawn")
            with ctx.Pool(self.workers, initializer=_worker_init) as pool:
                for code, bars, adjs, e in pool.imap_unordered(
                    worker_fetch, [(c, m, end) for c, m in items], chunksize=4
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

        old_shsz = sum(1 for s in self.stocks.values() if s.mkt != "bj")
        can_remove = n_shsz >= 0.9 * old_shsz  # 列表明显不全时不删除任何股票
        for code in list(self.stocks):
            s = self.stocks[code]
            if code in info:
                continue
            if s.mkt == "bj" and not bj_ok:
                continue
            if can_remove:
                del self.stocks[code]  # 已退市
        for code, d in info.items():
            s = self.stocks.get(code)
            if s is None:
                s = self.stocks[code] = Stock(code=code, mkt=d["mkt"])
            s.mkt = d["mkt"]
            s.name = d["name"] or s.name
            s.list_date = d["list_date"] or s.list_date
            s.is_st = "ST" in s.name.upper()
        self.note(f"股票列表：沪深 {n_shsz} 只，北交所 {len(info) - n_shsz} 只")

    # ---------- 全量构建 ----------
    def start_full_build(self) -> None:
        self.meta["build_pending"] = True
        self.meta["build_gen"] = self.today
        self.meta["build_end"] = self.src.latest_date()
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
        self.note(f"全量构建完成：沪深 {done} 只（失败 {len(failed)}），北交所 {done_bj} 只（失败 {len(failed_bj)}）")
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
        s = rebuild_from_history(prev, bars, adjs, built=gen)
        # 查询覆盖到了 end，即使最后几天停牌没有 K 线，也视为已处理到 end
        if s.last_seen < end:
            s.last_seen = end
        self.stocks[code] = s

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
            if len(bars) < 0.8 * len(listed):
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

    # ---------- 发行价 ----------
    def update_issue_prices(self, force: bool = False) -> None:
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
                self.note(f"发行价：东方财富数据匹配 {n} 只")
        for s in self.stocks.values():
            fill_issue_price(s)

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
        self.daily_bj()
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
    summary(f"- 发行价：东方财富 {sum(1 for s in stocks if s.issue_src == 'em')}，"
            f"首日前收盘推断 {sum(1 for s in stocks if s.issue_src == 'infer')}，"
            f"缺失 {sum(1 for s in stocks if not s.issue_src)}")
    if both:
        summary(f"- 校验：东方财富发行价与首日前收盘价一致 {agree}/{len(both)}")
