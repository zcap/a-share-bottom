"""港股、美股的网页数据：site/data/hk.json、us.json（网页读取）和 hk.csv、us.csv（可用 Excel 打开）。只用标准库。"""

from __future__ import annotations

import csv
import json
import math
import os

from .export import _r
from .gengine import SECTOR_CN, GStock, g_scores, hist_cut, is_pos

FIELDS = [
    "code", "mkt", "name", "board", "list_date", "last_trade", "close", "hfq", "adj",
    "min_low", "min_low_date", "min_close", "min_close_date",
    "issue_price", "issue_src", "issue_cmp", "first_date", "hist_cut", "warn",
    "mcap", "pe", "pb", "ps", "dy", "val_date", "industry", "sector",
    "roe", "gpm", "cfroe", "rev_yoy", "np_yoy", "vscore", "qscore", "gscore", "tscore",
]
CURRENCY = {"hk": "港元", "us": "美元"}
ISSUE_SRC_LABEL = {"em": "东方财富港股资料", "nasdaq": "纳斯达克 IPO 日历"}


def _fmt(v):
    return round(v, 2) if isinstance(v, float) and math.isfinite(v) else ""


def export_market(mkt: str, stocks: dict[str, GStock], meta: dict, site_dir: str = "site") -> dict:
    os.makedirs(os.path.join(site_dir, "data"), exist_ok=True)
    ready = [s for s in stocks.values() if s.has_data() and not s.excluded]
    ready.sort(key=lambda s: s.ratio())
    market_date = meta.get("market_date") or max((s.last_trade for s in ready), default="")
    scores = g_scores(ready)

    def sc(code, k):
        v = scores.get(code, {}).get(k)
        return round(v, 1) if v is not None else None

    rows = []
    for s in ready:
        rows.append([
            s.code, s.mkt, s.name or s.name_en, s.board, s.list_date, s.last_trade,
            _r(s.last_close, 6), _r(s.last_close, 6), 1,
            _r(s.min_low), s.min_low_date, _r(s.min_close), s.min_close_date,
            _r(s.issue_price, 6) if is_pos(s.issue_price) else None, s.issue_src,
            _r(s.issue_cmp) if is_pos(s.issue_cmp) else None, s.first_date,
            1 if hist_cut(s) else 0, s.jump_date,
            _r(s.mcap_now(), 4), _r(s.pe, 5), _r(s.pb, 5), _r(s.ps, 5), _r(s.dy, 4), s.fin_date,
            s.industry, SECTOR_CN.get(s.sector, s.sector),
            _r(s.roe, 4), _r(s.gpm, 4), _r(s.cfroe, 4), _r(s.rev_yoy, 4), _r(s.np_yoy, 4),
            sc(s.code, "value"), sc(s.code, "quality"), sc(s.code, "growth"), sc(s.code, "total"),
        ])
    doc = {
        "market": mkt,
        "currency": CURRENCY[mkt],
        "updated_at": meta.get("updated_at", ""),
        "market_date": market_date,
        "build_pending": False,
        "total": len(rows),
        "fields": FIELDS,
        "rows": rows,
    }
    with open(os.path.join(site_dir, "data", f"{mkt}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(site_dir, "data", f"{mkt}.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        cur = CURRENCY[mkt]
        w.writerow([
            "代码", "名称", "英文名", "上市板块/交易所", f"收盘价({cur})", "最后成交日",
            "历史最低价(日内最低,含发行价,按当前价格口径)", "最低价日期", "比值(现价/历史最低)", "距历史最低涨幅%",
            "历史最低收盘价(按当前价格口径)", "最低收盘日期", "比值(收盘口径,含发行价)",
            "发行价(原始)", "发行价(按当前价格口径)", "发行价来源", "发行价未采用原因", "上市日期", "数据起始日",
            "历史可能不完整", "单日涨跌超过4倍的日期",
            "综合分(价值50%+质量30%+成长20%)", "价值分(同行业内,越高越便宜)", "质量分", "成长分", "行业大类", "行业",
            f"总市值({cur})", "市盈率TTM", "市净率", "市销率TTM", "股息率%", "估值财务日期",
            "净资产收益率%", "毛利率%", "经营现金流/净资产%", "营收同比%(最近季度)", "盈利同比%(最近季度)",
            "最新一天来自腾讯行情(Yahoo还没有这一天)",
        ])
        for s in ready:
            base_l, date_l, base_c = s.min_low, s.min_low_date, s.min_close
            if is_pos(s.issue_cmp):
                if s.issue_cmp < base_l:
                    base_l, date_l = s.issue_cmp, "发行价"
                base_c = min(base_c, s.issue_cmp)
            rl, rc = max(1.0, s.last_close / base_l), max(1.0, s.last_close / base_c)
            w.writerow([
                s.code, s.name, s.name_en, s.board, _r(s.last_close, 6), s.last_trade,
                round(base_l, 4), date_l, round(rl, 4), round((rl - 1) * 100, 2),
                round(s.min_close, 4), s.min_close_date, round(rc, 4),
                s.issue_price if is_pos(s.issue_price) else "", round(s.issue_cmp, 4) if is_pos(s.issue_cmp) else "",
                ISSUE_SRC_LABEL.get(s.issue_src, ""), s.issue_note, s.list_date, s.first_date,
                "是" if hist_cut(s) else "", s.jump_date,
                *(("" if sc(s.code, k) is None else sc(s.code, k)) for k in ("total", "value", "quality", "growth")),
                SECTOR_CN.get(s.sector, s.sector), s.industry,
                *(_fmt(v) for v in (s.mcap_now(), s.pe, s.pb, s.ps, s.dy)), s.fin_date,
                *(_fmt(v) for v in (s.roe, s.gpm, s.cfroe, s.rev_yoy, s.np_yoy)),
                s.patched,
            ])
    return {"rows": len(rows), "market_date": market_date}
