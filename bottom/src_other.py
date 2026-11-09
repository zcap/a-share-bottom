"""其他免费数据源：腾讯行情（北交所 + 实时价）、北交所股票列表、东方财富新股发行价。

北交所 BaoStock 不覆盖，所以北交所用腾讯的日线（不复权 + 后复权，两者相除得到复权因子）
和腾讯实时行情做每日更新。
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass

import requests

from .engine import Bar, is_pos
from .util import norm_date

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (compatible; a-share-bottom/1.0)"}


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


# ---------------- 腾讯实时行情 ----------------
@dataclass
class Quote:
    sym: str  # 如 sh600000
    name: str
    price: float
    preclose: float
    open: float
    high: float
    low: float
    volume: float  # 手
    date: str  # 行情时间对应的日期 YYYY-MM-DD
    time: str  # HH:MM:SS
    raw: tuple = ()  # 全部原始字段（估值字段的位置先和 BaoStock 核对再使用）

    def field(self, i: int) -> float:
        return _f(self.raw[i]) if i < len(self.raw) else math.nan

    def to_bar(self) -> Bar:
        traded = is_pos(self.price) and is_pos(self.volume)
        return Bar(self.date, self.open, self.high, self.low, self.price, self.preclose, traded)

    def sane(self) -> bool:
        """字段位置是否解析正确（用于检测腾讯改格式）。"""
        if not is_pos(self.preclose) or len(self.date) != 10:
            return False
        if not is_pos(self.price):
            return True  # 停牌时现价可能为 0
        return self.low <= self.price + 1e-6 <= self.high + 2e-6 and 0.4 < self.price / self.preclose < 2.5


_QT_RE = re.compile(r'v_([a-z]{2}\d{6})="(.*?)";', re.S)


def parse_quotes(text: str) -> dict[str, Quote]:
    out = {}
    for sym, payload in _QT_RE.findall(text):
        f = payload.split("~")
        if len(f) < 35:
            continue
        ts = f[30]
        out[sym] = Quote(
            sym=sym,
            name=f[1],
            price=_f(f[3]),
            preclose=_f(f[4]),
            open=_f(f[5]),
            high=_f(f[33]),
            low=_f(f[34]),
            volume=_f(f[6]),
            date=norm_date(ts[:8]) if len(ts) >= 8 else "",
            time=f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}" if len(ts) >= 14 else "",
            raw=tuple(f),
        )
    return out


def tencent_quotes(syms: list[str], batch: int = 60, pause: float = 0.2) -> dict[str, Quote]:
    """批量取腾讯实时行情，syms 形如 sh600000 / sz000001 / bj920001。"""
    out: dict[str, Quote] = {}
    for i in range(0, len(syms), batch):
        chunk = syms[i : i + batch]
        for attempt in range(3):
            try:
                r = requests.get("https://qt.gtimg.cn/q=" + ",".join(chunk), headers=UA, timeout=20)
                r.raise_for_status()
                r.encoding = "gbk"
                out.update(parse_quotes(r.text))
                break
            except requests.RequestException as e:
                log.warning("腾讯行情请求失败（第 %d 次）：%s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        time.sleep(pause)
    return out


# ---------------- 北交所 ----------------
def bj_stock_list() -> list[dict]:
    """[{code, name, list_date}]"""
    import akshare as ak

    df = ak.stock_info_bj_name_code()
    out = []
    for code, name, ld in zip(df["证券代码"], df["证券简称"], df["上市日期"]):
        out.append({"code": str(code).zfill(6), "name": str(name), "list_date": norm_date(ld)})
    return out


def bj_history(code: str, list_date: str = "") -> tuple[list[Bar], list[float]]:
    """腾讯日线：不复权 + 后复权，相除得到每日复权因子，再归一化到第一根 K 线为 1。"""
    import akshare as ak

    start = (list_date or "1990-01-01").replace("-", "")
    sym = "bj" + code
    raw = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, adjust="")
    time.sleep(0.3)
    hfq = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, adjust="hfq")
    if raw is None or raw.empty:
        return [], []
    hfq_close = {}
    if hfq is not None and not hfq.empty:
        hfq_close = {norm_date(d): _f(c) for d, c in zip(hfq["date"], hfq["close"])}
    bars, adjs = [], []
    base = None
    prev_ratio, cur_adj = None, 1.0
    for row in raw.itertuples(index=False):
        d = norm_date(row.date)
        c = _f(row.close)
        if not d or not is_pos(c):
            continue
        h = hfq_close.get(d, math.nan)
        ratio = h / c if is_pos(h) else (prev_ratio or 1.0)
        if base is None:
            base = ratio
        # 复权因子只在除权除息日跳变；腾讯价格四舍五入会带来小抖动，0.3% 以内视为没变
        if prev_ratio is None or abs(ratio / prev_ratio - 1) >= 0.003:
            cur_adj = ratio / base
            prev_ratio = ratio
        bars.append(Bar(d, _f(row.open), _f(row.high), _f(row.low), c, math.nan, _f(row.volume) > 0))
        adjs.append(cur_adj)
    return bars, adjs


# ---------------- 发行价 ----------------
def _pick(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    raise KeyError(f"找不到列 {names}，现有列：{list(df.columns)}")


def issue_prices() -> dict[str, float]:
    """东方财富数据中心的新股发行价（2010 年以后上市的沪深股票 + 北交所）。"""
    import akshare as ak

    out: dict[str, float] = {}
    try:
        df = ak.stock_dxsyl_em()
        for c, p in zip(_pick(df, "股票代码", "代码"), _pick(df, "发行价", "发行价格")):
            if is_pos(_f(p)):
                out[str(c).zfill(6)] = _f(p)
    except Exception as e:  # noqa: BLE001
        log.warning("沪深发行价获取失败：%s", e)
    try:
        df = ak.stock_xgsglb_em(symbol="北交所")
        for c, p in zip(_pick(df, "代码", "股票代码"), _pick(df, "发行价格", "发行价")):
            if is_pos(_f(p)):
                out.setdefault(str(c).zfill(6), _f(p))
    except Exception as e:  # noqa: BLE001
        log.warning("北交所发行价获取失败：%s", e)
    return out


# ---------------- 财务数据（质量分、成长分、行业） ----------------
EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}
EM_PAGE_SIZE = 200  # 每页条数小一些，单次响应小，不容易在传输中被断开

# 业绩报表（RPT_LICO_FN_CPD）的字段名；对不上时按 AKShare 记录的字段顺序取（见 _EM_POS）
_EM_NAMES = {
    "code": "SECURITY_CODE", "roe": "WEIGHTAVG_ROE", "rev_yoy": "YSTZ", "np_yoy": "SJLTZ",
    "bps": "BPS", "ocfps": "MGJYXJJE", "gpm": "XSMLL", "industry": "PUBLISHNAME",
}
_EM_POS = {"code": 0, "roe": 12, "rev_yoy": 13, "np_yoy": 14, "bps": 15, "ocfps": 16, "gpm": 17, "industry": 22}


def _em_get(params: dict, tries: int = 5, url: str = EM_DATACENTER) -> dict:
    """请求东方财富数据中心。每次新建连接（复用连接时偶尔会被对方中途断开），失败按指数退避重试。"""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=EM_HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:  # 含 ChunkedEncodingError、JSON 不完整
            last = e
            log.warning("东方财富数据中心请求失败（第 %d 次）：%s: %s", attempt + 1, type(e).__name__, e)
            time.sleep(min(30, 2 ** attempt) + 0.5 * attempt)
    raise last


def _em_rows_to_records(rows: list[dict]) -> list[dict]:
    """按字段名取值；字段名对不上时按字段顺序取。取到的数还要经过 pipeline 里的交叉核对才会使用。"""
    if not rows:
        return []
    keys = list(rows[0].keys())
    by_name = all(v in rows[0] for v in _EM_NAMES.values())
    if not by_name:
        log.warning("业绩报表字段名和预期不同，改按字段顺序读取：%s", keys[:25])
    out = []
    for row in rows:
        vals = list(row.values())

        def get(k):
            if by_name:
                return row.get(_EM_NAMES[k])
            i = _EM_POS[k]
            return vals[i] if i < len(vals) else None

        code = str(get("code") or "").strip()
        if not code:
            continue
        ind = get("industry")
        ind = "" if ind is None or str(ind).strip() in ("", "nan", "None", "-") else str(ind).strip()
        rec = {"code": code.zfill(6), "industry": ind}
        for k in ("bps", "roe", "gpm", "ocfps", "rev_yoy", "np_yoy"):
            rec[k] = _f(get(k))
        out.append(rec)
    return out


def financials(period: str) -> list[dict]:
    """东方财富数据中心“业绩报表”：某一报告期全部 A 股（含北交所）的财务指标，分页取完。

    period 形如 2026-09-30。返回 [{code, industry, bps, roe, gpm, ocfps, rev_yoy, np_yoy}]。
    数据中心接口不在东方财富行情服务器的限流范围内。"""
    params = {
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "pageSize": str(EM_PAGE_SIZE),
        "pageNumber": "1",
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": f"(REPORTDATE='{period}')",
    }
    first = _em_get(params)
    result = first.get("result") or {}
    pages = int(result.get("pages") or 0)
    rows = list(result.get("data") or [])
    for page in range(2, pages + 1):
        time.sleep(0.5)
        data = _em_get({**params, "pageNumber": str(page)})
        rows.extend((data.get("result") or {}).get("data") or [])
    recs = _em_rows_to_records(rows)
    # 分页过程中数据更新可能导致个别重复，按代码去重
    return list({r["code"]: r for r in recs}.values())


# ---------------- 东方财富 F10 资料（发行价） ----------------
EM_SECURITIES = "https://datacenter.eastmoney.com/securities/api/data/v1/get"


def em_f10_all(report: str, source: str, columns: str = "ALL", page_size: int = 500) -> list[dict]:
    """不加条件分页取完东方财富 F10 某张表（按证券代码排序，避免翻页时顺序变化导致漏行）。"""
    base = {"reportName": report, "columns": columns, "pageSize": str(page_size), "source": source, "client": "PC",
            "sortColumns": "SECUCODE", "sortTypes": "1"}
    first = _em_get({**base, "pageNumber": "1"}, url=EM_SECURITIES)
    if not first.get("result"):  # 个别表不支持排序参数时去掉排序再试
        base.pop("sortColumns"), base.pop("sortTypes")
        first = _em_get({**base, "pageNumber": "1"}, url=EM_SECURITIES)
    result = first.get("result") or {}
    rows = list(result.get("data") or [])
    pages = int(result.get("pages") or 0)
    for page in range(2, pages + 1):
        time.sleep(0.3)
        data = _em_get({**base, "pageNumber": str(page)}, url=EM_SECURITIES)
        rows.extend((data.get("result") or {}).get("data") or [])
    total = int(result.get("count") or 0)
    uniq = {str(r.get("SECUCODE")): r for r in rows}
    if total and len(uniq) < 0.98 * total:
        raise RuntimeError(f"{report} 只取到 {len(uniq)} / {total} 条，数据不完整")
    return list(uniq.values())


_SWAP = re.compile(r"换股|吸收合并")


def issue_info_f10() -> dict[str, dict]:
    """东方财富 F10“发行相关”：全部 A 股（含 1990 年代上市、含北交所、含已退市）的每股发行价。

    返回 {代码: {price, list_date, first_close, swap}}；swap=True 表示换股吸收合并等方式上市，没有真正的发行价。"""
    rows = em_f10_all("RPT_PCF10_ORG_ISSUEINFO", source="HSF10")
    if rows:
        log.info("东方财富发行资料字段：%s", list(rows[0].keys()))
    out: dict[str, dict] = {}
    for d in rows:
        code = str(d.get("SECUCODE") or "").split(".")[0]
        if not re.fullmatch(r"\d{6}", code):
            continue
        # 只在“发行方式 / 发行类型”之类的字段里找“换股”，避免误判
        way_keys = [k for k in d if re.search(r"WAY|TYPE|METHOD|MODE", k)]
        texts = " ".join(str(d[k]) for k in way_keys if isinstance(d[k], str))
        out[code] = {
            "price": _f(d.get("ISSUE_PRICE")),
            "list_date": norm_date(d.get("LISTING_DATE")),
            "first_close": _f(d.get("CLOSE_PRICE")),
            "swap": bool(_SWAP.search(texts)),
        }
    return out
