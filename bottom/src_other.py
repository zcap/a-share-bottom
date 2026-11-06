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
