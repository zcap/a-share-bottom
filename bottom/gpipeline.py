"""港股、美股的运行流程：刷新官方列表 → 发行价资料 → 下载完整历史并重算 → 腾讯行情核对 → 保存。

每个市场有独立的状态目录（GitHub 上分别保存在 data-hk、data-us 分支）。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import os
import time
from collections import Counter

from .gengine import GStock, choose_ipo, compute, cutoff_date, hist_cut, is_pos, settled, spac_like
from .util import add_days, bj_now, summary

LIST_GUARD = 0.9  # 官方列表比上次少一成以上时，认为列表不完整，不删除股票
COMPLETE_RATIO = 0.95  # 下载成功比例达到它才算“全部完成”
EM_REFRESH_DAYS = 7  # 港股招股价资料每周刷新
IPO_BACKFILL_FROM = "2000-01"  # 美股 IPO 日历最早回溯到这个月（纳斯达克大约从 2000 年前后开始有数据）
IPO_BACKFILL_MIN = 12  # 每次运行回溯 IPO 日历最多用几分钟（第一次运行约需 15 分钟，会分两次完成）
BENCH = {"hk": ["0700.HK", "0005.HK"], "us": ["AAPL", "MSFT"]}
MARKET_NAME = {"hk": "港股", "us": "美股"}
IPO_FILE = "ipo_us.csv.gz"


class GlobalSources:
    """真实数据源（测试时替换成假的）。"""

    def __init__(self, mkt: str):
        self.mkt = mkt

    def stock_list(self):
        from . import src_global as g

        return g.hk_list() if self.mkt == "hk" else g.us_list()

    def histories(self, items, workers, deadline):
        from .src_global import yahoo_histories

        return yahoo_histories(items, workers=workers, deadline=deadline)

    def latest_date(self):
        from .src_global import latest_bar_date

        return latest_bar_date(BENCH[self.mkt])

    def quotes(self, syms):
        from .src_global import tencent_quotes

        return tencent_quotes(syms)

    def hk_issue_info(self):
        from .src_global import hk_issue_info

        return hk_issue_info()

    def us_ipo_month(self, ym):
        from .src_global import us_ipo_month

        return us_ipo_month(ym)


def _months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


class GlobalUpdater:
    def __init__(self, mkt: str, src, stocks: dict[str, GStock], meta: dict, state_dir: str = "state",
                 now: dt.datetime | None = None, deadline: float | None = None, workers: int = 8):
        self.mkt, self.src, self.stocks, self.meta = mkt, src, stocks, meta
        self.state_dir = state_dir
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.today = self.now.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")  # 北京时间
        self.deadline = deadline
        self.workers = workers
        self.notes: list[str] = []
        self.stats: dict = {}
        self.ipo: dict[str, list[dict]] = {}

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def time_left(self) -> float:
        return (self.deadline - time.time()) if self.deadline else 1e9

    # ---------- 股票列表 ----------
    def refresh_universe(self) -> None:
        try:
            lst = self.src.stock_list()
        except Exception as e:  # noqa: BLE001
            self.note(f"⚠️ 官方股票列表获取失败，沿用上次的列表：{type(e).__name__}: {e}")
            return
        old = set(self.stocks)
        allow_remove = bool(lst) and len(lst) >= LIST_GUARD * len(old)
        if not allow_remove and old:
            self.note(f"⚠️ 官方列表只有 {len(lst)} 只（上次 {len(old)} 只），可能不完整：只加入新股，不删除")
        added = []
        for code, info in lst.items():
            s = self.stocks.get(code)
            if s is None:
                s = self.stocks[code] = GStock(code=code, mkt=self.mkt, added=self.today)
                added.append(code)
            s.name_en, s.board = info.get("name_en", ""), info.get("board", "")
            if not s.name:
                s.name = s.name_en
            if info.get("fund"):
                s.excluded = "fund"
            elif s.excluded == "fund":
                s.excluded = ""
        removed = sorted(old - set(lst)) if allow_remove else []
        for code in removed:
            del self.stocks[code]
        if old:  # 第一次运行不列出“新增”
            if added:
                self.note(f"新上市（新出现在官方列表）{len(added)} 只：" + "、".join(added[:15]) + (" 等" if len(added) > 15 else ""))
            if removed:
                self.note(f"退市（从官方列表消失）{len(removed)} 只：" + "、".join(removed[:15]) + (" 等" if len(removed) > 15 else ""))
        self.stats["list"] = len(lst)

    # ---------- 发行价资料 ----------
    def update_issue_sources(self, force: bool = False) -> None:
        if self.mkt == "hk":
            self._hk_issue(force)
        else:
            self._load_ipo()
            self._us_ipo(force)

    def _hk_issue(self, force: bool) -> None:
        last = self.meta.get("em_issue_date", "")
        missing = [s for s in self.stocks.values() if not s.list_date and s.added >= add_days(self.today, -30)]
        due = force or not last or last <= add_days(self.today, -EM_REFRESH_DAYS) or (missing and last != self.today)
        if not due:
            return
        try:
            info = self.src.hk_issue_info()
        except Exception as e:  # noqa: BLE001
            self.note(f"⚠️ 东方财富港股资料获取失败，发行价沿用上次：{type(e).__name__}: {e}")
            return
        if len(info) < 0.8 * len(self.stocks):
            self.note(f"⚠️ 东方财富港股资料只有 {len(info)} 条，数据不完整，本次不使用")
            return
        n = 0
        for code, rec in info.items():
            s = self.stocks.get(code)
            if s is None:
                continue
            n += 1
            s.list_date = rec.get("list_date") or s.list_date
            if rec.get("name") and (not s.name or s.name == s.name_en):
                s.name = rec["name"]
            if is_pos(rec.get("price")):
                s.issue_price, s.issue_src, s.issue_date = rec["price"], "em", rec.get("list_date", "")
            elif s.issue_src == "em":
                s.issue_price, s.issue_src, s.issue_date = float("nan"), "", ""
        self.meta["em_issue_date"] = self.today
        self.note(f"东方财富港股资料：匹配 {n} 只，其中有招股价 {sum(1 for s in self.stocks.values() if s.issue_src == 'em')} 只")

    def _ipo_path(self) -> str:
        return os.path.join(self.state_dir, IPO_FILE)

    def _load_ipo(self) -> None:
        self.ipo = {}
        p = self._ipo_path()
        if os.path.exists(p):
            with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    r["price"] = float(r["price"])
                    self.ipo.setdefault(r["sym"], []).append(r)

    def _save_ipo(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        rows = sorted((r for rs in self.ipo.values() for r in rs), key=lambda r: (r["date"], r["sym"]))
        tmp = self._ipo_path() + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["sym", "date", "price", "name"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in ("sym", "date", "price", "name")})
        os.replace(tmp, self._ipo_path())

    def _us_ipo(self, force: bool) -> None:
        """纳斯达克 IPO 日历：每次刷新最近 3 个月；更早的月份逐步回溯到 2000 年，取到后不再重复取。"""
        done = set(self.meta.get("ipo_months", []))
        this_month = self.today[:7]
        recent = _months(add_days(self.today, -62)[:7], this_month)
        todo = recent + [m for m in reversed(_months(IPO_BACKFILL_FROM, this_month)) if m not in done and m not in recent]
        if force:
            todo = recent + [m for m in reversed(_months(IPO_BACKFILL_FROM, this_month)) if m not in recent]
        t_end = time.time() + IPO_BACKFILL_MIN * 60
        got, fails = 0, 0
        for ym in todo:
            if ym not in recent and (time.time() > t_end or self.time_left() < 20 * 60):
                break
            try:
                rows = self.src.us_ipo_month(ym)
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails >= 3:
                    self.note(f"⚠️ 纳斯达克 IPO 日历连续失败，下次再试：{type(e).__name__}: {e}")
                    break
                continue
            fails = 0
            for r in rows:
                lst = self.ipo.setdefault(r["sym"], [])
                if not any(x["date"] == r["date"] for x in lst):
                    lst.append(r)
                    got += 1
            if ym not in recent:  # 最近 3 个月每次都重新取（可能有新定价），更早的取到就不再重复
                done.add(ym)
            time.sleep(0.3)
        self.meta["ipo_months"] = sorted(done)
        self._save_ipo()
        left = len([m for m in _months(IPO_BACKFILL_FROM, this_month) if m not in done]) - len(recent)
        self.note(f"纳斯达克 IPO 日历：新增 {got} 条，共 {sum(len(v) for v in self.ipo.values())} 条"
                  + (f"；还有 {left} 个月没回溯完，下次运行继续" if left > 0 else ""))

    # ---------- 历史行情 ----------
    def download(self, cutoff: str) -> None:
        from .src_global import yahoo_symbol

        # 最久没更新的排在前面：万一被限流或时间不够，下次运行会先补它们
        todo = sorted((s for s in self.stocks.values() if s.excluded != "fund"), key=lambda s: (s.fetched, s.code))
        items = [(s.code, yahoo_symbol(self.mkt, s.code)) for s in todo]
        t0 = time.time()
        ok, errors = 0, Counter()
        failed = []
        for code, res, err in self.src.histories(items, self.workers, self.deadline):
            s = self.stocks[code]
            if res is None:
                s.fails += 1
                errors[err.split("：")[0]] += 1
                failed.append(code)
                continue
            bars, splits, n_div = res
            if self.mkt == "us":
                self._match_ipo(s, bars, cutoff)
            if not compute(s, bars, splits, n_div, cutoff):
                s.fails += 1
                errors["没有有效数据"] += 1
                failed.append(code)
                continue
            s.fails, s.fetched = 0, self.today
            ok += 1
            if self.mkt == "us":
                s.excluded = "spac" if spac_like(s.name_en, bars, cutoff) else ""
        self.stats.update(tried=len(items), ok=ok, secs=time.time() - t0)
        self.meta["complete"] = ok >= COMPLETE_RATIO * max(1, len(items))
        msg = f"下载完整历史：成功 {ok} / {len(items)} 只，用时 {(time.time() - t0) / 60:.1f} 分钟"
        if errors:
            msg += "；失败原因 " + "，".join(f"{k} {v}" for k, v in errors.most_common(5))
            msg += "；失败示例 " + "、".join(sorted(failed)[:10])
        self.note(msg)

    def _match_ipo(self, s: GStock, bars, cutoff: str) -> None:
        first = next((b.date for b in bars if is_pos(b.close) and b.date <= cutoff), "")
        rec = choose_ipo(self.ipo.get(s.code.upper(), []), first) if first else None
        if rec:
            s.issue_price, s.issue_src, s.issue_date = rec["price"], "nasdaq", rec["date"]
            s.list_date = first
        elif s.issue_src == "nasdaq":
            s.issue_price, s.issue_src, s.issue_date = float("nan"), "", ""

    # ---------- 腾讯行情：中文名 + 收盘价核对 ----------
    def tencent_check(self) -> None:
        from .src_global import tencent_symbol

        live = [s for s in self.stocks.values() if s.excluded != "fund"]
        syms = {tencent_symbol(self.mkt, s.code): s for s in live}
        try:
            quotes = self.src.quotes(list(syms))
        except Exception as e:  # noqa: BLE001
            self.note(f"⚠️ 腾讯行情获取失败（只影响中文名）：{type(e).__name__}: {e}")
            return
        same = agree = named = 0
        for sym, q in quotes.items():
            s = syms.get(sym)
            if s is None:
                continue
            name = q.get("name", "")
            if name and any("一" <= ch <= "鿿" for ch in name):
                s.name = name
                named += 1
            if q.get("date") == s.last_trade and is_pos(q.get("price")) and is_pos(s.last_close):
                same += 1
                agree += abs(q["price"] / s.last_close - 1) < 0.005
        self.stats["tencent"] = (len(quotes), same, agree)
        msg = f"腾讯行情：返回 {len(quotes)} / {len(syms)} 只，中文名 {named} 只"
        if same:
            msg += f"；同一交易日收盘价和 Yahoo 一致 {agree}/{same}（{agree / same:.1%}）"
            if same >= 50 and agree < 0.9 * same:
                msg = "⚠️ " + msg + "，一致比例偏低，请检查数据"
        self.note(msg)

    # ---------- 总入口 ----------
    def run(self, mode: str = "daily") -> None:
        force = mode == "full"
        self.refresh_universe()
        self.update_issue_sources(force=force)
        cutoff = cutoff_date(self.mkt, self.now)
        done = self.meta.get("settled_date", "")  # 已经用稳定数据算过的最后一天
        if mode == "daily" and self.meta.get("complete") and done:
            latest = self.src.latest_date()
            latest = min(latest, cutoff) if latest else ""
            if latest and latest <= done:
                self.note(f"Yahoo 还没有比 {done} 更新的收盘数据，本次不重新下载")
                self.meta["updated_at"] = bj_now().strftime("%Y-%m-%d %H:%M")
                return
        self.download(cutoff)
        # 收盘后不久的运行，Yahoo 当天的数据可能还会修正：不标记为稳定，补跑时再下载一次
        self.meta["settled_date"] = cutoff if self.meta.get("complete") and settled(self.mkt, self.now, cutoff) else ""
        self.tencent_check()
        dates = [s.last_trade for s in self.stocks.values() if s.has_data() and s.fetched == self.today]
        if dates:
            self.meta["market_date"] = max(dates)
        self.meta["updated_at"] = bj_now().strftime("%Y-%m-%d %H:%M")


def report(up: GlobalUpdater) -> None:
    mk = MARKET_NAME[up.mkt]
    stocks = list(up.stocks.values())
    shown = [s for s in stocks if s.has_data() and not s.excluded]
    summary(f"## {mk}历史最低价比值 · 本次运行")
    summary("")
    for n in up.notes:
        summary(f"- {n}")
    ex = Counter(s.excluded for s in stocks if s.excluded)
    summary(f"- 官方列表 {len(stocks)} 只；网页显示 {len(shown)} 只；数据日期 {up.meta.get('market_date', '')}")
    if ex:
        ex_names = {k: [s.name_en or s.code for s in stocks if s.excluded == k][:8] for k in ex}
        summary(f"- 不显示：SPAC（并购前空壳）{ex.get('spac', 0)} 只，例如 {('、'.join(ex_names.get('spac', [])) or '无')}；"
                f"封闭式基金 {ex.get('fund', 0)} 只，例如 {('、'.join(ex_names.get('fund', [])) or '无')}")
    has_ip = [s for s in shown if is_pos(s.issue_price)]
    used = [s for s in has_ip if is_pos(s.issue_cmp)]
    notes = Counter((s.issue_note.split(" ")[0] if s.issue_note else "") for s in has_ip if not is_pos(s.issue_cmp))
    summary(f"- 发行价：有发行价 {len(has_ip)} 只，换算后采用 {len(used)} 只"
            + (f"；未采用 {len(has_ip) - len(used)} 只（" + "，".join(f"{k or '其他'} {v}" for k, v in notes.most_common(3)) + "）"
               if len(has_ip) > len(used) else ""))
    bad = [s for s in has_ip if s.issue_note.startswith("发行价")]
    if bad:
        summary("- 发行价和首日收盘价相差过大的示例：" + "；".join(f"{s.code} {s.issue_note}" for s in bad[:5]))
    cut = sum(1 for s in shown if hist_cut(s))
    summary(f"- 历史数据可能没有覆盖到上市日的：{cut} 只（{'Yahoo 港股历史最早到 2000 年' if up.mkt == 'hk' else '上市很早的老股票'}）")
    jumps = [s for s in shown if s.jump_date]
    summary(f"- 数据检查：单日涨跌超过 4 倍的 {len(jumps)} 只（网页上标 ⚠，可能是拆合股没有复权），"
            f"修正异常最低价 {sum(s.n_fix for s in shown)} 根")
    stale = [s for s in shown if s.fetched != up.today]
    if stale:
        summary(f"- 本次没有下载成功、沿用上次结果的：{len(stale)} 只")
