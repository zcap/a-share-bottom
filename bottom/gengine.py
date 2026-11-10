"""港股、美股的核心计算（纯计算，不联网，只用标准库，方便测试）。

和 A 股不同，港美股每天直接用 Yahoo 的完整历史从头重算，不做增量：
下载完整历史和只下载最近几天的请求次数一样多，从头重算能自动反映新的分红、拆合股和数据源的事后修正。

Yahoo 的价格口径
----------------
* Open / High / Low / Close 已按拆股、合股调整（以今天的股本为准）；
* Adj Close 在此基础上再按分红等比调整，最后一天 Adj Close = Close。
所以 Low × (Adj Close ÷ Close) 就是“今天口径”的等比复权最低价，可以直接和现价比较。
比值 = 现价 ÷ 历史最低，和 A 股用后复权算出的结果口径一致（等比复权下前后复权的比值相同）。

发行价换算到今天口径：发行价 ÷ 上市后累计拆股倍数 × 上市首日的分红调整系数。
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass, replace

from .engine import Record, is_pos


@dataclass
class YBar:
    """Yahoo 的一根日线。"""

    date: str
    open: float
    high: float
    low: float
    close: float  # 已按拆合股调整
    adjclose: float  # 再按分红调整
    volume: float = math.nan  # 成交量；没有这个信息时为 nan


@dataclass
class GStock(Record):
    """一只港股或美股的状态（每天用完整历史重算后保存结果）。价格都是“今天口径”。"""

    code: str  # 港股 5 位代码（00700）；美股代码（AAPL、BRK.B）
    mkt: str  # hk / us
    name: str = ""  # 中文名（腾讯行情），没有时用英文名
    name_en: str = ""  # 官方列表里的英文名
    board: str = ""  # 港股：主板 / 创业板；美股：交易所
    added: str = ""  # 第一次出现在官方列表的日期
    list_date: str = ""  # 上市日期（港股来自东方财富；美股来自 IPO 日历）
    issue_price: float = math.nan  # 发行价（原始价格）
    issue_src: str = ""  # em = 东方财富港股资料；nasdaq = 纳斯达克 IPO 日历
    issue_date: str = ""  # 来源里的上市日 / 定价日
    issue_cmp: float = math.nan  # 发行价换算到今天口径（可以直接和历史最低比较）
    issue_note: str = ""  # 有发行价但没有采用的原因
    first_date: str = ""  # 数据里的第一根 K 线
    last_trade: str = ""  # 最后一次有成交的日期（停牌、没人买卖的日子 Yahoo 也有日线，但成交量是 0）
    last_bar: str = ""  # Yahoo 最后一根日线的日期
    last_close: float = math.nan  # 最后一根日线的收盘价
    min_low: float = math.inf  # 历史最低价（日内最低，今天口径）
    min_low_date: str = ""
    min_close: float = math.inf  # 历史最低收盘价（今天口径）
    min_close_date: str = ""
    n_splits: int = 0
    n_divs: int = 0
    n_fix: int = 0  # 修正过的异常最低价根数
    jump_date: str = ""  # 最近一次单日涨跌超过 4 倍的日期（可能是拆合股没有复权）
    excluded: str = ""  # spac / fund / temp：不显示
    patched: str = ""  # 最新一天来自腾讯行情（Yahoo 的完整历史还没有这一天）时，记下这一天的日期
    fetched: str = ""  # 最近一次成功下载的日期
    fails: int = 0  # 连续下载失败次数
    # ---- 估值与财务（Yahoo，每周刷新；比率都是无单位的，避免报表币种和交易币种不同的问题）----
    sector: str = ""  # Yahoo 板块（11 个大类）
    industry: str = ""  # Yahoo 行业（约 150 个细分）
    pe: float = math.nan  # 市盈率 TTM（= 股价 ÷ 每股收益，亏损为负）
    pb: float = math.nan  # 市净率（净资产为负时为空）
    ps: float = math.nan  # 市销率 TTM
    dy: float = math.nan  # 股息率 %（最近 12 个月分红 ÷ 现价，用 Yahoo 历史里的分红算）
    roe: float = math.nan  # 净资产收益率 %（TTM）
    gpm: float = math.nan  # 毛利率 %
    cfroe: float = math.nan  # 经营现金流 ÷ 净资产 %
    rev_yoy: float = math.nan  # 营收同比 %（最近一个季度）
    np_yoy: float = math.nan  # 盈利同比 %（最近一个季度）
    mcap: float = math.nan  # 取数时的总市值（交易币种）
    mcap_px: float = math.nan  # 取数时的股价（用来按最新收盘价换算市值）
    fin_cur: str = ""  # 报表币种（和交易币种不同时，Yahoo 的市销率没有换算汇率）
    ps_raw: float = math.nan  # Yahoo 给的市销率（原始值）
    ps_imp: float = math.nan  # 市盈率 × 净利率 推算出的市销率（盈利的公司才有）
    fin_date: str = ""  # 最近一次取估值与财务数据的日期
    fin_fails: int = 0

    def has_data(self) -> bool:
        return is_pos(self.last_close) and math.isfinite(self.min_low)

    def mcap_now(self) -> float:
        """按最新收盘价换算的总市值。"""
        if is_pos(self.mcap) and is_pos(self.mcap_px) and is_pos(self.last_close):
            return self.mcap * self.last_close / self.mcap_px
        return math.nan

    def ratio(self, use_issue: bool = True) -> float:
        base = self.min_low
        if use_issue and is_pos(self.issue_cmp):
            base = min(base, self.issue_cmp)
        return max(1.0, self.last_close / base)


JUMP = 4.0  # 单日涨跌超过 4 倍视为可疑跳变
LOW_FIX = 0.5  # 最低价比开盘、收盘中较低者还低一半以上，视为错误成交，改用开盘/收盘中较低者
ISSUE_RANGE = {"hk": (0.2, 3.0), "us": (0.1, 3.0)}  # 发行价 ÷ 上市首日收盘价 的合理范围
ISSUE_DAYS = (-3, 10)  # 数据第一天 − 上市日（或定价日）的允许天数
DY_MAX = 25.0  # 股息率上限（%）


def _days(a: str, b: str) -> int:
    return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days


def _traded(b: YBar, prev: YBar | None) -> bool:
    """这一天有没有成交。停牌或没人买卖的日子，Yahoo 的日线成交量是 0、开高低收都等于前一天的收盘价。

    没有成交量信息时按有成交算；成交量是 0 但价格动了（数据缺成交量），也按有成交算。"""
    if not (isinstance(b.volume, (int, float)) and math.isfinite(b.volume)) or b.volume > 0:
        return True
    if is_pos(b.high) and is_pos(b.low) and not math.isclose(b.high, b.low, rel_tol=1e-9):
        return True
    return prev is not None and not math.isclose(b.close, prev.close, rel_tol=1e-9)


def compute(s: GStock, bars: list[YBar], splits: list[tuple[str, float]], divs: list[tuple[str, float]],
            cutoff: str) -> bool:
    """用完整历史重算一只股票。cutoff 之后的 K 线（盘中未收盘）不计入。没有有效数据返回 False，状态不变。

    divs：Yahoo 历史里的分红 [(除息日, 每股金额)]，金额已按拆合股调整到今天口径。"""
    clean, f_prev = [], None
    for b in bars:
        if b.date > cutoff or not is_pos(b.close):
            continue
        if is_pos(b.adjclose):
            f_prev = b.adjclose / b.close
        elif f_prev is None:
            continue
        else:  # 个别 K 线没有复权价（常见于最新一天）：沿用前一天的分红调整系数
            b = replace(b, adjclose=b.close * f_prev)
        clean.append(b)
    bars = clean
    if not bars:
        return False
    min_low, min_low_date, min_close, min_close_date = math.inf, "", math.inf, ""
    n_fix, jump, traded = 0, "", ""
    prev = None
    for b in bars:
        if _traded(b, prev):
            traded = b.date
        f = b.adjclose / b.close
        ref = min(b.close, b.open) if is_pos(b.open) else b.close
        lo = b.low if is_pos(b.low) else ref
        if lo < LOW_FIX * ref:
            lo, n_fix = ref, n_fix + 1
        lo = min(lo, ref)  # 最低价不会高于开盘、收盘价（数据错误时）
        if lo * f < min_low:
            min_low, min_low_date = lo * f, b.date
        if b.adjclose < min_close:
            min_close, min_close_date = b.adjclose, b.date
        if prev is not None:
            r = b.close / prev.close
            if r > JUMP or r < 1 / JUMP:
                jump = b.date
        prev = b
    last = bars[-1]
    k = last.close / last.adjclose  # 正常为 1；统一到“最后收盘价”口径
    s.first_date, s.last_bar, s.last_close = bars[0].date, last.date, last.close
    s.last_trade = traded or last.date
    s.patched = ""
    s.min_low, s.min_low_date = min_low * k, min_low_date
    s.min_close, s.min_close_date = min_close * k, min_close_date
    s.n_splits = sum(1 for d, r in splits if d <= cutoff and is_pos(r) and r != 1)
    divs = [(d, a) for d, a in divs if d <= cutoff and is_pos(a)]
    s.n_divs, s.n_fix, s.jump_date = len(divs), n_fix, jump
    # 股息率：最近 12 个月的分红 ÷ 最新收盘价（同一价格口径）；超过 25% 多是特别分红，不计
    year_ago = (dt.date.fromisoformat(last.date) - dt.timedelta(days=365)).isoformat()
    dy = 100 * sum(a for d, a in divs if d > year_ago) / last.close
    s.dy = dy if dy <= DY_MAX else math.nan
    s.issue_cmp, s.issue_note = math.nan, ""
    if is_pos(s.issue_price):
        convert_issue(s, bars[0], splits, k)
    return True


def convert_issue(s: GStock, first: YBar, splits: list[tuple[str, float]], k: float = 1.0) -> None:
    """把发行价换算到今天口径，并检查它确实属于这段历史（避免代码被重复使用、数据缺了开头几年）。"""
    if s.issue_date:
        gap = _days(first.date, s.issue_date)
        if not ISSUE_DAYS[0] <= gap <= ISSUE_DAYS[1]:
            s.issue_note = f"数据从 {first.date} 开始，和上市日 {s.issue_date} 对不上"
            return
    cum = 1.0
    for d, r in splits:
        if d > first.date and is_pos(r):
            cum *= r
    raw_first_close = first.close * cum
    lo, hi = ISSUE_RANGE.get(s.mkt, (0.1, 3.0))
    if not lo <= s.issue_price / raw_first_close <= hi:
        s.issue_note = f"发行价 {s.issue_price:g} 和上市首日收盘价 {raw_first_close:.4g} 相差过大"
        return
    s.issue_cmp = s.issue_price / cum * (first.adjclose / first.close) * k


PATCH_TOL = 0.005  # 腾讯的昨收和已有的最后收盘价相差在 0.5% 以内，才认为两边接得上


def patch_check(s: GStock, q: dict, cutoff: str) -> str:
    """腾讯行情能不能用来补上 Yahoo 缺的最新一天。返回 ""（不需要或没有成交）、"ok"、"mismatch"（接不上，不补）。

    Yahoo 的完整历史对港股常常晚几个小时才有当天的日线。腾讯行情给了当天的收盘、最高、最低和昨收：
    昨收和我们已有的最后收盘价一致，说明中间没有缺日子、也没有拆股合股，可以把这一天接上去。"""
    d = q.get("date") or ""
    if not (s.has_data() and s.last_trade and s.last_trade < d <= cutoff):
        return ""
    price, pc, hi, lo = (q.get(k) for k in ("price", "preclose", "high", "low"))
    if not (is_pos(price) and is_pos(pc) and is_pos(hi) and is_pos(lo)):
        return ""  # 当天没有成交（停牌）
    if abs(pc / s.last_close - 1) > PATCH_TOL or not (1 / JUMP < price / pc < JUMP):
        return "mismatch"
    if not (lo <= hi and lo <= price * 1.0001 and price <= hi * 1.0001):
        return "mismatch"  # 最高、最低和现价对不上，字段可能有问题，不用
    return "ok"


def patch_apply(s: GStock, q: dict) -> None:
    """把腾讯行情的这一天接到已有结果后面（先用 patch_check 确认可以接）。

    当天如果正好除息，这里不做分红调整，比值会略偏低（最多相差这次分红的幅度）；Yahoo 补上这一天后重算时自动修正。"""
    price = q["price"]
    lo = min(q["low"], price)
    if lo < LOW_FIX * price:
        lo = price
    if lo < s.min_low:
        s.min_low, s.min_low_date = lo, q["date"]
    if price < s.min_close:
        s.min_close, s.min_close_date = price, q["date"]
    if math.isfinite(s.dy):
        s.dy = s.dy * s.last_close / price
    s.last_close, s.last_trade, s.patched = price, q["date"], q["date"]


def common_date(stocks) -> str:
    """多数股票的最后交易日（并列时取较晚的）。用它作为“数据日期”，少数股票领先或落后都不影响。"""
    cnt: dict[str, int] = {}
    for s in stocks:
        if s.has_data() and not s.excluded and s.last_trade:
            cnt[s.last_trade] = cnt.get(s.last_trade, 0) + 1
    return max(cnt, key=lambda d: (cnt[d], d)) if cnt else ""


def choose_ipo(records: list[dict], first_date: str) -> dict | None:
    """同一代码可能先后属于不同公司：选定价日和数据第一天最接近、且在允许范围内的那条 IPO 记录。"""
    best, best_gap = None, None
    for r in records:
        if not r.get("date") or not is_pos(r.get("price")):
            continue
        gap = _days(first_date, r["date"])
        if ISSUE_DAYS[0] <= gap <= ISSUE_DAYS[1] and (best_gap is None or abs(gap) < best_gap):
            best, best_gap = r, abs(gap)
    return best


# ---------------- 美股：SPAC 和封闭式基金 ----------------
SPAC_NAME = re.compile(r"\b(acquisition|merger)\b|blank check|\bspac\b", re.I)
FUND_NAME = re.compile(r"\bfunds?\b|closed[- ]end|\bmunicipal\b|\bmuni\b|total return", re.I)
TRUST_FUND = re.compile(
    r"\b(income|bond|credit|dividend|equity|opportunit\w*|infrastructure|strategic|premium|tax[- ]advantaged|"
    r"utilit\w*|science and technology)\b.*\btrust\b", re.I)
NOT_FUND = re.compile(r"realty|real estate|\breit\b|royalty|propert|mortgage|residential|apartment|hotel|lodging|"
                      r"storage|net lease|bancorp|\bbank", re.I)


def is_fund(name: str) -> bool:
    """封闭式基金（按名称判断）：名称带 Fund，或“收益/债券/股息……Trust”这类（房地产、矿权信托除外）。"""
    n = name or ""
    if FUND_NAME.search(n):
        return True
    return bool(TRUST_FUND.search(n)) and not NOT_FUND.search(n)


def spac_like(name: str, bars: list[YBar], today: str) -> bool:
    """并购前的 SPAC：近 4 年上市，股价一直贴着信托价值（约 10 美元）几乎不动。

    完成并购后股价开始正常波动，就不再满足条件，会自动显示出来。"""
    closes = [b.close for b in bars if is_pos(b.close)]
    if not closes or _days(today, bars[0].date) > 4 * 366:
        return False
    if len(closes) >= 60 and all(9.0 <= c <= 12.5 for c in closes):
        rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        if statistics.median(rets) <= 0.004:
            return True
    return bool(SPAC_NAME.search(name or "")) and all(8.0 <= c <= 15.0 for c in closes)


def shell_like(s: GStock) -> bool:
    """Yahoo 把并购前的 SPAC 归在“Shell Companies”行业；股价还在 10 美元附近的按 SPAC 处理。"""
    return s.industry == "Shell Companies" and is_pos(s.last_close) and 8.0 <= s.last_close <= 15.0


# ---------------- 交易时间 ----------------
MARKET_TZ = {"hk": "Asia/Hong_Kong", "us": "America/New_York"}
DAY_FINAL = {"hk": "16:30", "us": "16:45"}  # 当地时间过了这个点，当天的日线才算收盘定稿


def cutoff_date(mkt: str, now_utc: dt.datetime) -> str:
    """只使用已经收盘的日线：收盘定稿前运行时，不计入今天这根（盘中价格）。"""
    from zoneinfo import ZoneInfo

    local = now_utc.astimezone(ZoneInfo(MARKET_TZ[mkt]))
    d = local.date()
    if local.strftime("%H:%M") < DAY_FINAL[mkt]:
        d -= dt.timedelta(days=1)
    return d.isoformat()


DAY_SETTLED = {"hk": "19:00", "us": "19:30"}  # 收盘后过了这个点，认为 Yahoo 的当日数据已经稳定（不会再修正）


def settled(mkt: str, now_utc: dt.datetime, cutoff: str) -> bool:
    """这次用到的最后一天数据是否已经稳定：运行时间在当地 DAY_SETTLED 之后，或已经是第二天。"""
    from zoneinfo import ZoneInfo

    local = now_utc.astimezone(ZoneInfo(MARKET_TZ[mkt]))
    return local.date().isoformat() > cutoff or local.strftime("%H:%M") >= DAY_SETTLED[mkt]


HIST_START = {"hk": "2000-01-31", "us": "1962-01-31"}  # Yahoo 历史数据的大致起点


def hist_cut(s: GStock) -> bool:
    """历史数据可能没有覆盖到上市日：上市日早于数据起点，或数据正好从 Yahoo 的最早日期开始。"""
    if not s.first_date:
        return False
    if s.list_date:
        return _days(s.first_date, s.list_date) > ISSUE_DAYS[1]
    return s.first_date <= HIST_START.get(s.mkt, "")


# ---------------- 估值与财务（Yahoo 的 info） ----------------
MARKET_CURRENCY = {"hk": "HKD", "us": "USD"}
SECTOR_CN = {
    "Technology": "科技", "Financial Services": "金融", "Healthcare": "医疗保健", "Consumer Cyclical": "可选消费",
    "Consumer Defensive": "必需消费", "Industrials": "工业", "Basic Materials": "原材料", "Energy": "能源",
    "Utilities": "公用事业", "Real Estate": "房地产", "Communication Services": "通信服务",
}
PS_CHECK_TOL = 0.3  # Yahoo 市销率和“市盈率 × 净利率”推算值相差超过三成时，用推算值


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def _in(v: float, lo: float, hi: float) -> float:
    return v if math.isfinite(v) and lo <= v <= hi else math.nan


def apply_info(s: GStock, info: dict, today: str) -> list[str]:
    """把 Yahoo 的 info 写进状态，返回核对时发现的问题（用于统计）。

    只用无单位的比率或同一币种的数：
    * 市盈率 = 股价 ÷ 每股收益（Yahoo 的每股收益已换算成交易币种；亏损时为负，排名时排最差）；
    * 市销率：报表币种和交易币种不同时，Yahoo 的市销率不一定换算过，用“市盈率 × 净利率”核对；
    * 经营现金流 ÷ 净资产：净资产 = 净利润 ÷ 净资产收益率，三者都是报表币种。"""
    flags: list[str] = []
    cur = str(info.get("currency") or "").upper()
    fin_cur = str(info.get("financialCurrency") or cur).upper()
    s.sector = str(info.get("sector") or "").strip()
    s.industry = str(info.get("industry") or "").strip()
    s.fin_date = today
    price = _num(info.get("currentPrice")) if is_pos(_num(info.get("currentPrice"))) else _num(info.get("regularMarketPrice"))
    s.mcap, s.mcap_px = _num(info.get("marketCap")), price
    for k in ("pe", "pb", "ps", "ps_raw", "ps_imp", "roe", "gpm", "cfroe", "rev_yoy", "np_yoy"):
        setattr(s, k, math.nan)
    s.fin_cur = ""
    if cur and cur != MARKET_CURRENCY.get(s.mkt, cur):
        flags.append("交易币种不符")  # 比如美元柜台，价格和市值口径不一致，估值不用
        s.mcap = math.nan
    else:
        eps, ype = _num(info.get("trailingEps")), _num(info.get("trailingPE"))
        if is_pos(price) and math.isfinite(eps) and eps != 0:
            pe = price / eps
            if is_pos(ype) and abs(pe / ype - 1) > 0.05:
                flags.append("每股收益和市盈率对不上")
                pe = ype
            s.pe = pe if abs(pe) <= 5000 else math.nan
        elif is_pos(ype):
            s.pe = ype
        pb = _num(info.get("priceToBook"))
        s.pb = pb if is_pos(pb) and pb <= 1000 else math.nan  # 净资产为负（大额回购）时视为缺失，不当成最便宜
        s.fin_cur = fin_cur
        s.ps_raw = _in(_num(info.get("priceToSalesTrailing12Months")), 1e-9, 1e9)
        margin = _num(info.get("profitMargins"))
        if is_pos(s.pe) and is_pos(margin):
            s.ps_imp = s.pe * margin
        s.ps = ps_value(s)
        if is_pos(s.ps_imp) and is_pos(s.ps_raw) and abs(s.ps_raw / s.ps_imp - 1) > PS_CHECK_TOL:
            flags.append("币种不同、市销率用推算值" if fin_cur != cur else "市销率改用推算值")
        elif fin_cur != cur and is_pos(s.ps_raw) and not is_pos(s.ps_imp):
            flags.append("币种不同、亏损，市销率按汇率系数换算")
    roe = _num(info.get("returnOnEquity"))
    s.roe = _in(roe * 100, -500, 500)
    gm = _num(info.get("grossMargins"))
    s.gpm = _in(gm * 100, -500, 100) if gm != 0 else math.nan  # 银行等没有毛利率，Yahoo 给 0
    ni, ocf = _num(info.get("netIncomeToCommon")), _num(info.get("operatingCashflow"))
    if math.isfinite(roe) and roe != 0 and math.isfinite(ni) and ni != 0 and math.isfinite(ocf):
        equity = ni / roe
        if equity > 0:
            s.cfroe = _in(100 * ocf / equity, -1000, 1000)
    s.rev_yoy = _in(_num(info.get("revenueGrowth")) * 100, -100, 10000)
    s.np_yoy = _in(_num(info.get("earningsGrowth")) * 100, -10000, 100000)
    return flags


def ps_value(s: GStock, fx: dict | None = None) -> float:
    """市销率。Yahoo 的市销率 = 市值（交易币种）÷ 营收（报表币种），两个币种不同时没有换算汇率；
    市盈率用的每股收益是换算过的，所以“市盈率 × 净利率”才是口径一致的市销率。

    * 能推算（盈利）：币种不同就用推算值；币种相同以 Yahoo 的为准，两者相差超过三成时用推算值。
    * 不能推算（亏损）：币种相同用 Yahoo 的；币种不同时除以汇率系数 fx[报表币种]（见 ps_fx），没有系数就不用。"""
    mismatch = bool(s.fin_cur) and s.fin_cur != MARKET_CURRENCY.get(s.mkt, s.fin_cur)
    if is_pos(s.ps_imp):
        if mismatch or not is_pos(s.ps_raw) or abs(s.ps_raw / s.ps_imp - 1) > PS_CHECK_TOL:
            v = s.ps_imp
        else:
            v = s.ps_raw
    elif not mismatch:
        v = s.ps_raw
    else:
        k = (fx or {}).get(s.fin_cur)
        v = s.ps_raw / k[0] if k and is_pos(s.ps_raw) else math.nan
    return _in(v, 1e-6, 1000)


PS_FX_MIN = 8  # 一种报表币种至少有这么多只能核对的股票，才反推它的汇率系数
PS_FX_NEAR = 0.6  # 其中至少六成的比值在中位数的 ±15% 以内，才认为这个系数可靠


def ps_fx(stocks) -> dict[str, tuple[float, int]]:
    """反推汇率系数：{报表币种: (系数, 样本数)}。

    对报表币种和交易币种不同、又能推算市销率的股票，“Yahoo 的市销率 ÷ 推算值”就是两种货币的汇率
    （比如港股里用人民币出报表的公司约为 1.09，用美元的约为 7.8）。取中位数，用来换算亏损公司的市销率。"""
    by: dict[str, list[float]] = {}
    for s in stocks:
        if s.fin_cur and s.fin_cur != MARKET_CURRENCY.get(s.mkt, s.fin_cur) and is_pos(s.ps_raw) and is_pos(s.ps_imp):
            by.setdefault(s.fin_cur, []).append(s.ps_raw / s.ps_imp)
    out = {}
    for cur, rs in by.items():
        if len(rs) < PS_FX_MIN:
            continue
        k = statistics.median(rs)
        if sum(1 for r in rs if abs(r / k - 1) <= 0.15) >= PS_FX_NEAR * len(rs):
            out[cur] = (k, len(rs))
    return out


def apply_ps_fx(stocks) -> tuple[dict, int]:
    """按反推的汇率系数，补上“币种不同、亏损”的公司的市销率。返回（系数表, 补上的只数）。"""
    stocks = [s for s in stocks if s.fin_date and s.fin_cur]
    fx = ps_fx(stocks)
    n = 0
    for s in stocks:
        if s.fin_cur != MARKET_CURRENCY.get(s.mkt, s.fin_cur) and not is_pos(s.ps_imp):
            s.ps = ps_value(s, fx)
            n += is_pos(s.ps)
    return fx, n


def metric_value_g(s: GStock, name: str) -> float:
    """港美股评分指标（越大越好）。"""
    if name == "ep":
        return 1.0 / s.pe if math.isfinite(s.pe) and s.pe != 0 else math.nan
    if name == "bp":
        return 1.0 / s.pb if is_pos(s.pb) else math.nan
    if name == "sp":
        return 1.0 / s.ps if is_pos(s.ps) else math.nan
    v = getattr(s, name)
    return v if isinstance(v, (int, float)) and math.isfinite(v) else math.nan


G_SCORE_DEFS = {
    # 价值：盈利收益率、账面市值比、销售市值比、股息率（港股高股息特征明显，所以加入股息率）
    "value": (("ep", "bp", "sp", "dy"), 2),
    "quality": (("roe", "gpm", "cfroe"), 2),
    "growth": (("rev_yoy", "np_yoy"), 1),
}


def g_scores(stocks) -> dict[str, dict[str, float]]:
    """港美股评分：先比同行业，行业太小用同板块，再不够用全市场。没有估值财务数据的不参与。"""
    from .engine import compute_scores

    pool = [s for s in stocks if s.has_data() and not s.excluded and s.fin_date]
    return compute_scores(pool, defs=G_SCORE_DEFS, metric=metric_value_g, group_keys=("sector", "industry"),
                          exclude=lambda s: False)
