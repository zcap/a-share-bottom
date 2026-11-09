"""港股、美股数据源（全部免费、无需注册）。

* 股票列表：美股用 Nasdaq Trader 官方代码表；港股用港交所官方证券列表。
* 历史行情：Yahoo Finance（yfinance），每只股票 1 次请求取完整历史，含分红、拆合股。
* 中文名、收盘价核对：腾讯行情。
* 发行价：港股用东方财富港股资料（招股价）；美股用纳斯达克 IPO 日历。
"""

from __future__ import annotations

import io
import logging
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

import requests

from .gengine import YBar, is_fund
from .util import norm_date

log = logging.getLogger(__name__)

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}


def _f(x) -> float:
    try:
        v = float(str(x).replace(",", "").replace("$", "").strip())
        return v if math.isfinite(v) else math.nan
    except (TypeError, ValueError):
        return math.nan


def _get(url: str, params=None, headers=None, timeout: int = 60, tries: int = 4) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or BROWSER, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            log.warning("请求失败（第 %d 次）%s：%s", i + 1, url, e)
            time.sleep(3 * (i + 1))
    raise last


# ======================= 股票列表 =======================
NASDAQ_FILES = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
# 单位（SPAC 的 Units）要去掉，但合伙制企业的 Common Units / Depositary Units 是普通股性质，保留
US_EXCLUDE = re.compile(
    r"\b(warrants?|rights?|preferred|pfd|notes?|debentures?|due \d{4}|trust preferred|subordinated|"
    r"ETNs?|exchange[- ]traded)\b|(?<!common )(?<!depositary )\bunits?\b|depositary shares,? each representing a 1/",
    re.I)
US_BOARD = {"Q": "纳斯达克", "N": "纽交所", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe", "V": "IEX"}
_US_NAME_TAIL = re.compile(
    r"\s+(-\s+)?(common stock|common shares|ordinary shares|class [a-c] (common stock|ordinary shares|shares)|"
    r"american depositary shares?.*|ads.*|shares of beneficial interest|common units?.*)$", re.I)


def clean_us_name(name: str) -> str:
    n = re.split(r"\s+-\s+", name or "", maxsplit=1)[0]
    return _US_NAME_TAIL.sub("", n).strip() or (name or "").strip()


def parse_nasdaq_files(texts: list[str]) -> dict[str, dict]:
    """解析 Nasdaq Trader 代码表。只保留普通股（含 ADR），去掉 ETF、测试代码、权证、优先股、单位、票据等。"""
    out: dict[str, dict] = {}
    for text in texts:
        lines = [x for x in text.splitlines() if x.strip() and not x.startswith("File Creation Time")]
        if not lines:
            continue
        head = lines[0].split("|")
        for line in lines[1:]:
            d = dict(zip(head, line.split("|")))
            sym = (d.get("Symbol") or d.get("ACT Symbol") or "").strip()
            name = (d.get("Security Name") or "").strip()
            if not sym or "$" in sym or d.get("ETF") == "Y" or d.get("Test Issue") == "Y":
                continue
            if US_EXCLUDE.search(name):
                continue
            exch = d.get("Exchange", "Q")  # nasdaqlisted.txt 没有交易所列，都是纳斯达克
            # 用去掉后缀的名称判断基金：纳斯达克给业务发展公司（BDC，如 Ares Capital）加了“- Closed End Fund”后缀，
            # 但它们是放贷的经营性公司，按股票保留；名称本身带 Fund 的封闭式基金仍然排除
            base = clean_us_name(name)
            out[sym] = {"name_en": base, "board": US_BOARD.get(exch, "其他"), "fund": is_fund(base)}
    return out


def us_list() -> dict[str, dict]:
    texts = [_get(u).text for u in NASDAQ_FILES]
    if any("File Creation Time" not in t for t in texts):
        raise RuntimeError("Nasdaq Trader 代码表内容不完整")
    return parse_nasdaq_files(texts)


HKEX_LIST = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"


def parse_hkex_frame(raw) -> dict[str, dict]:
    """解析港交所证券列表（pandas 读出的原始表格）。只保留股本证券（主板 + 创业板），去掉人民币柜台（8 开头）。"""
    hdr = next(i for i in range(min(10, len(raw))) if any(str(v).strip() == "Stock Code" for v in raw.iloc[i].values))
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr].values]
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        if str(r.get("Category", "")).strip() != "Equity":
            continue
        sub = str(r.get("Sub-Category", ""))
        if "Equity Securities" not in sub:
            continue
        try:
            code = f"{int(float(r['Stock Code'])):05d}"
        except (TypeError, ValueError):
            continue
        if code.startswith("8"):
            continue
        out[code] = {"name_en": str(r.get("Name of Securities", "")).strip(),
                     "board": "创业板" if "GEM" in sub else "主板", "fund": False}
    return out


def hk_list() -> dict[str, dict]:
    import pandas as pd

    r = _get(HKEX_LIST)
    return parse_hkex_frame(pd.read_excel(io.BytesIO(r.content), header=None))


# ======================= Yahoo 历史行情 =======================
def yahoo_symbol(mkt: str, code: str) -> str:
    return f"{int(code):04d}.HK" if mkt == "hk" else code.replace(".", "-")


def frame_to_bars(df) -> tuple[list[YBar], list[tuple[str, float]], list[tuple[str, float]]]:
    """yfinance 的 DataFrame → (K 线, 拆合股 [(日期, 倍数)], 分红 [(除息日, 每股金额)])。同一天出现两行时保留后一行。"""
    if df is None or len(df) == 0:
        return [], [], []
    cols = set(df.columns)
    dates = [ts.strftime("%Y-%m-%d") for ts in df.index]

    def col(name, fallback=None):
        if name in cols:
            return [_f(x) for x in df[name].tolist()]
        return fallback if fallback is not None else [math.nan] * len(dates)

    o, h, lo, c = col("Open"), col("High"), col("Low"), col("Close")
    a = col("Adj Close", c)
    sp, dv = col("Stock Splits", [0.0] * len(dates)), col("Dividends", [0.0] * len(dates))
    by_date: dict[str, YBar] = {}
    splits: dict[str, float] = {}
    divs: dict[str, float] = {}
    for i, d in enumerate(dates):
        by_date[d] = YBar(d, o[i], h[i], lo[i], c[i], a[i])
        if math.isfinite(sp[i]) and sp[i] > 0 and sp[i] != 1:
            splits[d] = sp[i]
        if math.isfinite(dv[i]) and dv[i] > 0:
            divs[d] = dv[i]
    bars = [by_date[d] for d in sorted(by_date)]
    return bars, sorted(splits.items()), sorted(divs.items())


def _yf():
    import yfinance as yf

    yf.config.debug.hide_exceptions = False  # 出错时抛异常，由这里统一重试和统计
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    return yf


def yahoo_tz(sym: str) -> str:
    return "Asia/Hong_Kong" if sym.endswith(".HK") else "America/New_York"


def yahoo_history(sym: str, period: str = "max", timeout: int = 30):
    yf = _yf()
    t = yf.Ticker(sym)
    # 时区是已知的：预先填上，yfinance 就不用为每只股票多发一次查询时区的请求（请求数减半，更不容易被限流）
    t._tz = yahoo_tz(sym)
    from yfinance.exceptions import YFInvalidPeriodError

    try:
        df = t.history(period=period, interval="1d", auto_adjust=False, actions=True, timeout=timeout)
    except YFInvalidPeriodError:
        # 刚上市几天的股票，Yahoo 只接受 1d/5d 这类区间：改用起始日期取，结果同样是全部历史
        if period != "max":
            raise
        df = t.history(start="1970-01-02", interval="1d", auto_adjust=False, actions=True, timeout=timeout)
    return frame_to_bars(df)


RATE_LIMIT_STOP = 40  # 累计被限流这么多次就停止，剩下的沿用上次结果


class NoData(Exception):
    """Yahoo 上没有这只股票的数据（退市、代码不存在等）。"""


def _parallel(items: list[tuple[str, str]], fetch, workers: int, deadline: float | None, what: str
              ) -> Iterator[tuple[str, object | None, str]]:
    """并发请求 Yahoo。fetch(Yahoo 代码) 返回结果或抛异常；逐只产出 (代码, 结果 或 None, 错误说明)。

    被限流时退避重试，累计限流 RATE_LIMIT_STOP 次就停止，剩下的留给下次运行（沿用上次的结果）。"""
    from yfinance.exceptions import (YFPricesMissingError, YFRateLimitError, YFTickerMissingError,
                                     YFTzMissingError)

    _yf()
    stop = threading.Event()
    lock = threading.Lock()
    limited = [0]

    def one(code: str, sym: str):
        for attempt in range(4):
            if stop.is_set() or (deadline and time.time() > deadline):
                return code, None, "未请求（时间用完或被限流）"
            try:
                return code, fetch(sym), ""
            except YFRateLimitError:
                with lock:
                    limited[0] += 1
                    if limited[0] >= RATE_LIMIT_STOP:
                        stop.set()
                time.sleep(20 * (attempt + 1) + random.random() * 10)
            except (NoData, YFPricesMissingError, YFTzMissingError, YFTickerMissingError) as e:
                # 退市、代码在 Yahoo 上不存在；网络抖动时也会表现成这样，所以再试一次
                if attempt >= 1:
                    return code, None, f"没有数据：{str(e)[:80]}"
                time.sleep(3)
            except Exception as e:  # noqa: BLE001
                if attempt >= 2:
                    return code, None, f"{type(e).__name__}: {str(e)[:80]}"
                time.sleep(2 * (attempt + 1))
        return code, None, "被限流"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, c, s) for c, s in items]
        for fut in as_completed(futs):
            yield fut.result()
    if limited[0]:
        log.warning("Yahoo %s 限流 %d 次%s", what, limited[0], "，已提前停止" if stop.is_set() else "")


def _history_or_nodata(sym: str):
    bars, splits, divs = yahoo_history(sym)
    if not bars:
        raise NoData(sym)
    return bars, splits, divs


def yahoo_histories(items: list[tuple[str, str]], workers: int = 8, deadline: float | None = None
                    ) -> Iterator[tuple[str, tuple | None, str]]:
    """并发下载完整历史。items = [(代码, Yahoo 代码)]；逐只产出 (代码, (K线, 拆合股, 分红) 或 None, 错误说明)。"""
    return _parallel(items, _history_or_nodata, workers, deadline, "历史行情")


# Yahoo 的 quoteSummary：一次请求取估值、财务、行业（yfinance 的 info 要发 3 次请求，这里只取需要的模块）
QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
QS_MODULES = "financialData,defaultKeyStatistics,summaryDetail,assetProfile,price"


def flatten_summary(res: dict) -> dict:
    """quoteSummary 的结果按模块嵌套，摊平成一层；同名字段以靠前的模块为准。"""
    out: dict = {}
    for mod in QS_MODULES.split(","):
        d = res.get(mod)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, dict):
                v = v.get("raw")
            if k not in out and v is not None:
                out[k] = v
    return out


def yahoo_info(sym: str, timeout: int = 30) -> dict:
    yf = _yf()
    try:
        from yfinance.data import YfData

        params = {"modules": QS_MODULES, "formatted": "false", "corsDomain": "finance.yahoo.com", "symbol": sym,
                  "lang": "en-US", "region": "US"}
        j = YfData().get_raw_json(QS_URL + sym, params=params, timeout=timeout)
    except (ImportError, AttributeError, TypeError):
        # yfinance 内部接口变了：退回公开的 get_info()（多发两次请求，但结果一样）
        info = yf.Ticker(sym).get_info() or {}
        if not info.get("currency"):
            raise NoData(sym)
        return info
    except Exception as e:  # noqa: BLE001
        if "404" in str(e):
            raise NoData(sym) from e
        raise
    res = ((j.get("quoteSummary") or {}).get("result") or [None])[0]
    if not res:
        raise NoData(sym)
    return flatten_summary(res)


def yahoo_infos(items: list[tuple[str, str]], workers: int = 4, deadline: float | None = None
                ) -> Iterator[tuple[str, dict | None, str]]:
    """并发取估值与财务。逐只产出 (代码, info 或 None, 错误说明)。"""
    return _parallel(items, yahoo_info, workers, deadline, "估值财务")


def latest_bar_date(syms: list[str]) -> str:
    """几只基准股票在 Yahoo 上的最新日线日期（用来判断有没有新数据）。"""
    best = ""
    for sym in syms:
        try:
            bars, _, _ = yahoo_history(sym, period="5d")
            if bars:
                best = max(best, bars[-1].date)
        except Exception as e:  # noqa: BLE001
            log.warning("基准股票 %s 查询失败：%s", sym, e)
    return best


# ======================= 腾讯行情（中文名、收盘价核对） =======================
def tencent_symbol(mkt: str, code: str) -> str:
    return f"{mkt}{code}"


def parse_tencent(text: str) -> dict[str, dict]:
    """解析腾讯行情返回值。时间字段只保留数字（港股 2026/11/09 11:59:59、美股 2026-11-06 16:03:41）。"""
    out = {}
    for key, val in re.findall(r'v_([A-Za-z0-9_.]+)="(.*?)";', text, re.S):
        f = val.split("~")
        if len(f) < 35:
            continue
        ts = re.sub(r"\D", "", f[30])
        out[key] = {"name": f[1].strip(), "price": _f(f[3]), "preclose": _f(f[4]),
                    "date": norm_date(ts[:8]) if len(ts) >= 8 else ""}
    return out


def tencent_quotes(syms: list[str], batch: int = 60) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(syms), batch):
        part = syms[i:i + batch]
        try:
            r = _get("https://qt.gtimg.cn/q=" + ",".join(part), headers={"User-Agent": BROWSER["User-Agent"]},
                     timeout=20, tries=2)
            r.encoding = "gbk"
            got = parse_tencent(r.text)
            for s in part:  # 带点的美股代码，返回的变量名里点可能变成下划线
                q = got.get(s) or got.get(s.replace(".", "_"))
                if q:
                    out[s] = q
        except Exception as e:  # noqa: BLE001
            log.warning("腾讯行情批量请求失败：%s", e)
        time.sleep(0.15)
    return out


# ======================= 发行价 =======================
def hk_issue_info() -> dict[str, dict]:
    """东方财富港股资料：全部港股的招股价、上市日期、中文简称（一次分页取完）。"""
    from .src_other import em_f10_all

    rows = em_f10_all("RPT_HKF10_INFO_SECURITYINFO", source="F10",
                      columns="SECUCODE,SECURITY_NAME_ABBR,LISTING_DATE,ISSUE_PRICE")
    out = {}
    for d in rows:
        code = str(d.get("SECUCODE") or "").split(".")[0]
        if not re.fullmatch(r"\d{5}", code):
            continue
        out[code] = {"price": _f(d.get("ISSUE_PRICE")), "list_date": norm_date(d.get("LISTING_DATE")),
                     "name": str(d.get("SECURITY_NAME_ABBR") or "").strip()}
    return out


NASDAQ_HDR = {**BROWSER, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def _us_date(s: str) -> str:
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", str(s or "").strip())
    if m:
        return norm_date(f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}")
    return norm_date(s)


def parse_nasdaq_ipo(j: dict) -> list[dict]:
    rows = (((j.get("data") or {}).get("priced") or {}).get("rows")) or []
    out = []
    for r in rows:
        sym = str(r.get("proposedTickerSymbol") or "").strip().upper()
        price = _f(r.get("proposedSharePrice"))
        date = _us_date(r.get("pricedDate"))
        if sym and date and price > 0:
            out.append({"sym": sym, "price": price, "date": date, "name": str(r.get("companyName") or "").strip()})
    return out


def us_ipo_month(ym: str) -> list[dict]:
    """纳斯达克 IPO 日历某个月已定价的 IPO（含纽交所上市的）。ym 形如 2026-09。"""
    r = _get("https://api.nasdaq.com/api/ipo/calendar", params={"date": ym}, headers=NASDAQ_HDR, timeout=30, tries=3)
    return parse_nasdaq_ipo(r.json())
