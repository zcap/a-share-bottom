"""第 0 步：检查港股、美股（以及各市场发行价）数据源能否从 GitHub 访问、覆盖多少、数值对不对。

只读检查，不改动任何数据。结果写到运行摘要；更细的原始字段打印在日志里。
运行：python -m bottom probe-global
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import time
import traceback

import requests

from .util import summary

log = logging.getLogger(__name__)

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}
EM_SEC = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_WEB = "https://datacenter-web.eastmoney.com/api/data/v1/get"

RESULTS: list[tuple[str, bool]] = []
CTX: dict = {}  # 检查之间共享的数据（如股票列表）


def check(section: str, name: str):
    """装饰器：运行一项检查，把结果写进摘要。被装饰函数返回一行说明文字。"""

    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn()
                summary(f"- ✅ **{name}**（{time.time() - t0:.0f} 秒）：{detail}")
                RESULTS.append((f"{section}/{name}", True))
            except Exception as e:  # noqa: BLE001
                summary(f"- ❌ **{name}**（{time.time() - t0:.0f} 秒）：{type(e).__name__}: {str(e)[:300]}")
                log.info(traceback.format_exc())
                RESULTS.append((f"{section}/{name}", False))

        run.section = section
        return run

    return deco


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _get(url, params=None, headers=None, timeout=30, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or BROWSER, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _em(url, params):
    r = _get(url, params=params)
    r.raise_for_status()
    j = r.json()
    return j.get("result") or {}, j


# ======================= 股票列表（新股、退市识别） =======================
US_EXCLUDE = re.compile(
    r"\b(warrants?|units?|rights?|preferred|pfd|notes?|debentures?|due \d{4}|trust preferred|"
    r"subordinated|depositary shares,? each representing a 1/)", re.I)


@check("列表", "美股官方代码表（Nasdaq Trader）")
def us_list():
    rows = []
    for url in ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"):
        r = _get(url)
        r.raise_for_status()
        lines = [x for x in r.text.splitlines() if x and not x.startswith("File Creation Time")]
        head = lines[0].split("|")
        for line in lines[1:]:
            d = dict(zip(head, line.split("|")))
            d["_sym"] = d.get("Symbol") or d.get("ACT Symbol") or ""
            d["_exch"] = d.get("Exchange", "Q")  # nasdaqlisted 没有 Exchange 列，都是纳斯达克
            rows.append(d)
    total = len(rows)
    etf = sum(1 for d in rows if d.get("ETF") == "Y")
    test = sum(1 for d in rows if d.get("Test Issue") == "Y")
    common = [d for d in rows if d.get("ETF") != "Y" and d.get("Test Issue") != "Y"
              and "$" not in d["_sym"] and not US_EXCLUDE.search(d.get("Security Name", ""))]
    by_exch = {}
    for d in common:
        by_exch[d["_exch"]] = by_exch.get(d["_exch"], 0) + 1
    adr = sum(1 for d in common if re.search(r"american depositary|\bADS\b|\bADR\b", d.get("Security Name", ""), re.I))
    CTX["us"] = [d["_sym"] for d in common]
    log.info("美股样例：%s", [(d["_sym"], d.get("Security Name", "")[:40]) for d in common[:15]])
    return (f"共 {total} 条（ETF {etf}、测试代码 {test}）；过滤权证/优先股/单位等后普通股 {len(common)} 只，"
            f"其中 ADR 约 {adr} 只；按交易所 {json.dumps(by_exch, ensure_ascii=False)}"
            f"（Q=纳斯达克 N=纽交所 A=NYSE American P=Arca Z=Cboe）")


@check("列表", "港股官方证券列表（港交所）")
def hk_list():
    import pandas as pd

    url = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
    r = _get(url, timeout=60)
    r.raise_for_status()
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    hdr = next(i for i in range(10) if any(str(v).strip() == "Stock Code" for v in raw.iloc[i].values))
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr].values]
    asof = " ".join(str(v) for v in raw.iloc[:hdr].values.ravel() if str(v) != "nan")[:60]
    cats = df["Category"].value_counts().head(8).to_dict()
    eq = df[df["Category"].astype(str).str.strip() == "Equity"]
    subs = eq["Sub-Category"].value_counts().to_dict()
    main = eq[eq["Sub-Category"].astype(str).str.contains("Equity Securities", na=False)]
    codes = [str(c).strip().zfill(5) for c in main["Stock Code"]]
    rmb = [c for c in codes if c.startswith("8")]  # 人民币柜台等（8 开头）
    CTX["hk"] = [c for c in codes if not c.startswith("8")]
    return (f"{asof}；类别 {json.dumps(cats, ensure_ascii=False)}；Equity 子类别 {json.dumps(subs, ensure_ascii=False)}；"
            f"股本证券 {len(codes)} 只（其中 8 开头的人民币柜台等 {len(rmb)} 只），保留 {len(CTX['hk'])} 只")


# ======================= 历史行情（Yahoo） =======================
def _yf_sym_us(s):
    return s.replace(".", "-")


def _yf_sym_hk(c):
    return f"{int(c):04d}.HK"


@check("历史", "Yahoo 单只完整历史（看起始年份、拆股分红）")
def yahoo_single():
    import yfinance as yf

    out = []
    for sym in ("AAPL", "KO", "0700.HK", "0005.HK", "0001.HK", "9988.HK"):
        h = yf.Ticker(sym).history(period="max", auto_adjust=False, actions=True)
        if h is None or h.empty:
            out.append(f"{sym} 无数据")
            continue
        first = h.index[0].strftime("%Y-%m-%d")
        splits = int((h.get("Stock Splits", 0) != 0).sum())
        divs = int((h.get("Dividends", 0) != 0).sum())
        adj = h["Adj Close"] / h["Close"]
        lo = (h["Low"] * adj).min()
        ratio = h["Adj Close"].iloc[-1] / lo
        out.append(f"{sym} 从 {first} 起 {len(h)} 根，拆合股 {splits} 次、分红 {divs} 次，现价/历史最低 {ratio:.1f}")
        time.sleep(0.5)
    return "；".join(out)


def _batch_test(yf, syms, period, label):
    t0 = time.time()
    df = yf.download(syms, period=period, interval="1d", group_by="ticker", auto_adjust=False,
                     actions=True, threads=True, progress=False)
    secs = time.time() - t0
    ok = 0
    for s in syms:
        try:
            sub = df[s] if len(syms) > 1 else df
            if sub["Close"].dropna().shape[0] > 0:
                ok += 1
        except Exception:  # noqa: BLE001
            pass
    return ok, secs


@check("历史", "Yahoo 批量下载速度（估算全市场耗时）")
def yahoo_rate():
    import yfinance as yf

    us = CTX.get("us") or []
    hk = CTX.get("hk") or []
    if not us or not hk:
        raise RuntimeError("需要先取到美股、港股列表")
    us_s = [_yf_sym_us(s) for s in us[:: max(1, len(us) // 80)][:80]]
    hk_s = [_yf_sym_hk(c) for c in hk[:: max(1, len(hk) // 60)][:60]]
    parts = []
    for label, syms, n_all in (("美股完整历史", us_s, len(us)), ("港股完整历史", hk_s, len(hk))):
        ok, secs = _batch_test(yf, syms, "max", label)
        est = secs / max(len(syms), 1) * n_all / 60
        parts.append(f"{label} {ok}/{len(syms)} 只成功，用时 {secs:.0f} 秒，按此速度全市场约 {est:.0f} 分钟")
        time.sleep(3)
    syms = [_yf_sym_us(s) for s in us[:300]]
    ok, secs = _batch_test(yf, syms, "5d", "近 5 日")
    parts.append(f"美股近 5 日 {ok}/{len(syms)} 只，用时 {secs:.0f} 秒")
    return "；".join(parts)


# ======================= 腾讯港美股实时行情 =======================
def _tencent(syms):
    r = _get("https://qt.gtimg.cn/q=" + ",".join(syms), headers={"User-Agent": BROWSER["User-Agent"]})
    r.encoding = "gbk"
    return dict(re.findall(r'v_([A-Za-z0-9_.]+)="(.*?)";', r.text, re.S))


@check("行情", "腾讯港美股实时行情（字段、批量）")
def tencent_hkus():
    sample = ["hk00700", "hk09988", "hk00005", "usAAPL", "usBABA", "usBRK.B", "usMCD"]
    got = _tencent(sample)
    lines = []
    for k in sample:
        v = got.get(k.replace(".", "_")) or got.get(k)
        if not v:
            lines.append(f"{k} 无返回")
            continue
        f = v.split("~")
        # 前若干个字段在 A 股里依次是：名称、代码、现价、昨收、今开；时间在 30，最高 33，最低 34
        lines.append(f"{k}：{f[1]} 现价 {f[3]} 昨收 {f[4]} 高 {f[33] if len(f) > 34 else '?'} "
                     f"低 {f[34] if len(f) > 34 else '?'} 时间 {f[30] if len(f) > 30 else '?'}（{len(f)} 个字段）")
        log.info("腾讯 %s 原始字段：%s", k, {i: x for i, x in enumerate(f) if x not in ("", "0", "0.00")})
    n_ok = 0
    if CTX.get("hk"):
        batch = ["hk" + c for c in CTX["hk"][:60]]
        n_ok += len(_tencent(batch))
    if CTX.get("us"):
        batch = ["us" + s for s in CTX["us"][:60]]
        n_ok += len(_tencent(batch))
    return "；".join(lines) + f"；批量 120 只返回 {n_ok} 只"


# ======================= 发行价来源 =======================
@check("发行价-A股", "新浪“新股发行”逐只（核对 A 股推断值用）")
def a_sina():
    import akshare as ak

    out = []
    for code, expect in (("600519", "31.39"), ("000002", "?"), ("001979", "换股上市"), ("601766", "合并上市"), ("300750", "25.14")):
        t0 = time.time()
        try:
            df = ak.stock_ipo_info(stock=code)
            m = {str(a).strip(): str(b).strip() for a, b in zip(df.iloc[:, 0], df.iloc[:, 1])}
            price = next((v for k, v in m.items() if "发行价" in k), "无")
            date = next((v for k, v in m.items() if "上市日期" in k), "")
            out.append(f"{code} 发行价 {price} 上市 {date}（参考 {expect}，{time.time() - t0:.1f}s）")
        except Exception as e:  # noqa: BLE001
            out.append(f"{code} 失败 {type(e).__name__}")
        time.sleep(1)
    if all("失败" in x for x in out):
        raise RuntimeError("；".join(out))
    return "；".join(out)


@check("发行价-A股", "东方财富 A 股 F10 发行资料（逐只 / 批量）")
def a_em_f10():
    found = []
    for rpt in ("RPT_PCF10_ORG_ISSUEINFO", "RPT_F10_ORG_ISSUEINFO", "RPT_F10_PUBLIC_ISSUE"):
        try:
            res, _ = _em(EM_SEC, {"reportName": rpt, "columns": "ALL", "filter": '(SECUCODE="600519.SH")',
                                  "pageNumber": "1", "pageSize": "5", "source": "HSF10", "client": "PC"})
            data = res.get("data") or []
            if data:
                keys = [k for k in data[0] if "PRICE" in k or "LISTING" in k or "DATE" in k][:8]
                vals = {k: data[0][k] for k in keys}
                bulk, _ = _em(EM_SEC, {"reportName": rpt, "columns": "SECUCODE,ISSUE_PRICE", "pageNumber": "1",
                                       "pageSize": "500", "source": "HSF10", "client": "PC"})
                found.append(f"{rpt} 可用：茅台 {json.dumps(vals, ensure_ascii=False)}；不加条件批量查询共 {bulk.get('count')} 条")
            else:
                found.append(f"{rpt} 无数据")
        except Exception as e:  # noqa: BLE001
            found.append(f"{rpt} 失败 {type(e).__name__}")
    if not any("可用" in x for x in found):
        raise RuntimeError("；".join(found))
    return "；".join(found)


@check("发行价-A股", "东方财富新股数据最早覆盖到哪年（不加 2010 年限制）")
def a_em_ipo_history():
    res, _ = _em(EM_WEB, {"sortColumns": "LISTING_DATE", "sortTypes": "1", "pageSize": "5", "pageNumber": "1",
                          "reportName": "RPTA_APP_IPOAPPLY", "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,ISSUE_PRICE,LISTING_DATE",
                          "filter": "(LISTING_DATE>'1990-01-01')", "source": "WEB", "client": "WEB"})
    data = res.get("data") or []
    first = [(d.get("SECURITY_CODE"), d.get("SECURITY_NAME_ABBR"), d.get("ISSUE_PRICE"), str(d.get("LISTING_DATE"))[:10]) for d in data]
    return f"共 {res.get('count')} 条，最早几条：{first}"


@check("发行价-港股", "东方财富港股 F10 证券资料（招股价）")
def hk_em_f10():
    out = []
    for code, expect in (("00700", "3.70"), ("09988", "176"), ("01810", "17"), ("00005", "?"), ("02318", "10.83")):
        res, _ = _em(EM_SEC, {"reportName": "RPT_HKF10_INFO_SECURITYINFO", "columns": "SECUCODE,SECURITY_NAME_ABBR,LISTING_DATE,ISSUE_PRICE",
                              "filter": f'(SECUCODE="{code}.HK")', "pageNumber": "1", "pageSize": "5",
                              "source": "F10", "client": "PC"})
        d = (res.get("data") or [{}])[0]
        out.append(f"{code} {d.get('SECURITY_NAME_ABBR', '')} 招股价 {d.get('ISSUE_PRICE')} 上市 {str(d.get('LISTING_DATE'))[:10]}（参考 {expect}）")
        time.sleep(0.5)
    try:
        bulk, _ = _em(EM_SEC, {"reportName": "RPT_HKF10_INFO_SECURITYINFO", "columns": "SECUCODE,ISSUE_PRICE",
                               "pageNumber": "1", "pageSize": "500", "source": "F10", "client": "PC"})
        out.append(f"不加条件批量查询：共 {bulk.get('count')} 条，第一页 {len(bulk.get('data') or [])} 条")
    except Exception as e:  # noqa: BLE001
        out.append(f"批量查询失败 {type(e).__name__}")
    return "；".join(out)


@check("发行价-港股", "同花顺港股新股列表")
def hk_ths():
    import akshare as ak

    df = ak.stock_ipo_hk_ths()
    cols = list(df.columns)
    date_col = next((c for c in cols if "上市" in c and "日" in c), None)
    price_col = next((c for c in cols if "价" in c), None)
    earliest = str(df[date_col].min()) if date_col else "?"
    return f"{len(df)} 条，最早上市日 {earliest}；列：{cols[:10]}；价格列 {price_col}"


@check("发行价-港股", "港交所新上市报告（官方年度表格）")
def hk_hkex_nlr():
    out, ok = [], 0
    for y in (2025, 2015, 2005):
        for board in ("Main", "GEM"):
            url = (f"https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/New-Listings/New-Listing-Information/"
                   f"New-Listing-Report/{board}/NLR{y}_Eng.xlsx")
            try:
                r = requests.get(url, headers=BROWSER, timeout=30)
                out.append(f"{y}{board} HTTP {r.status_code}")
                ok += r.status_code == 200
            except requests.RequestException as e:
                out.append(f"{y}{board} {type(e).__name__}")
            time.sleep(0.3)
    if not ok:
        raise RuntimeError("链接都不可用（链接格式待调整）：" + "；".join(out))
    return "；".join(out)


NASDAQ_HDR = {**BROWSER, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


@check("发行价-美股", "纳斯达克 IPO 日历（按月回溯，找最早覆盖年份）")
def us_nasdaq_ipo():
    out = []
    sample = None
    for ym in ("2026-09", "2021-06", "2015-06", "2010-06", "2005-06", "2000-06", "1996-06"):
        try:
            r = _get("https://api.nasdaq.com/api/ipo/calendar", params={"date": ym}, headers=NASDAQ_HDR)
            j = r.json()
            priced = (((j.get("data") or {}).get("priced") or {}).get("rows")) or []
            out.append(f"{ym}：{len(priced)} 只")
            if priced and sample is None:
                sample = priced[0]
        except Exception as e:  # noqa: BLE001
            out.append(f"{ym}：{type(e).__name__}")
        time.sleep(0.5)
    if sample is None:
        raise RuntimeError("；".join(out))
    return "；".join(out) + f"；字段样例 {json.dumps(sample, ensure_ascii=False)[:300]}"


@check("发行价-美股", "Yahoo IPO 日历（回溯深度）")
def us_yahoo_ipo():
    import yfinance as yf

    out = []
    for start, end in (("2026-08-01", "2026-10-31"), ("2019-01-01", "2019-03-31"), ("2010-01-01", "2010-03-31")):
        df = yf.Calendars(start=start, end=end).get_ipo_info_calendar(limit=100)
        n = 0 if df is None else len(df)
        cols = [] if df is None else list(df.columns)[:8]
        out.append(f"{start[:7]}～{end[:7]}：{n} 条")
        if n and "cols" not in CTX:
            CTX["cols"] = cols
            log.info("Yahoo IPO 样例：%s", df.head(3).to_dict("records"))
        time.sleep(1)
    return "；".join(out) + f"；列 {CTX.get('cols')}"


# ======================= 估值与财务 =======================
INFO_KEYS = ("currency", "financialCurrency", "marketCap", "trailingPE", "priceToBook", "priceToSalesTrailing12Months",
             "enterpriseToEbitda", "dividendYield", "returnOnEquity", "grossMargins", "operatingCashflow",
             "revenueGrowth", "earningsGrowth", "bookValue", "trailingEps", "sector", "industry")


@check("估值", "Yahoo 单只估值与财务字段（看币种、负净资产）")
def yahoo_info():
    import yfinance as yf

    out = []
    for sym in ("AAPL", "MCD", "BABA", "9988.HK", "0700.HK", "0941.HK"):
        info = yf.Ticker(sym).info or {}
        vals = {k: info.get(k) for k in INFO_KEYS}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        eps = vals.get("trailingEps")
        pe_calc = (price / eps) if price and eps else None
        out.append(f"{sym}：" + "，".join(f"{k}={vals[k]}" for k in ("currency", "financialCurrency", "trailingPE",
                                                                    "priceToBook", "returnOnEquity", "sector"))
                   + (f"，按价格/EPS 算 PE={pe_calc:.1f}" if pe_calc else ""))
        log.info("Yahoo %s：%s", sym, vals)
        time.sleep(1)
    return "；".join(out)


@check("估值", "Yahoo 筛选器批量取全市场（估值批量来源）")
def yahoo_screen():
    import yfinance as yf
    from yfinance import EquityQuery

    out = []
    for region in ("hk", "us"):
        t0 = time.time()
        res = yf.screen(EquityQuery("eq", ["region", region]), size=250, offset=0)
        quotes = res.get("quotes") or []
        total = res.get("total")
        keys = sorted(quotes[0].keys()) if quotes else []
        want = [k for k in ("trailingPE", "priceToBook", "epsTrailingTwelveMonths", "bookValue", "marketCap",
                            "dividendYield", "trailingAnnualDividendYield", "averageAnalystRating") if k in keys]
        out.append(f"{region}：共 {total} 只，单页 {len(quotes)} 条用时 {time.time() - t0:.1f}s，含估值字段 {want}")
        log.info("筛选器 %s 字段：%s", region, keys)
        time.sleep(1)
    return "；".join(out)


ALL = [us_list, hk_list, yahoo_single, yahoo_rate, tencent_hkus,
       a_sina, a_em_f10, a_em_ipo_history, hk_em_f10, hk_ths, hk_hkex_nlr, us_nasdaq_ipo, us_yahoo_ipo,
       yahoo_info, yahoo_screen]

TITLES = {"列表": "股票列表（新股、退市识别）", "历史": "历史行情（Yahoo）", "行情": "实时行情（腾讯）",
          "发行价-A股": "发行价：A 股", "发行价-港股": "发行价：港股", "发行价-美股": "发行价：美股",
          "估值": "估值与财务（第 2 步评分用）"}


def probe_global() -> int:
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    summary("## 港股、美股数据源检查（第 0 步）")
    last = None
    for fn in ALL:
        if fn.section != last:
            summary("")
            summary(f"### {TITLES.get(fn.section, fn.section)}")
            last = fn.section
        fn()
    ok = sum(1 for _, v in RESULTS if v)
    summary("")
    summary(f"共 {len(RESULTS)} 项，通过 {ok} 项。这一步只做检查，不影响现有 A 股数据和网页。")
    return 0
