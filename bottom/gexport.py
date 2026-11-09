"""港股、美股的网页数据：site/data/hk.json、us.json（网页读取）和 hk.csv、us.csv（可用 Excel 打开）。只用标准库。"""

from __future__ import annotations

import csv
import json
import os

from .export import _r
from .gengine import GStock, hist_cut, is_pos

FIELDS = [
    "code", "mkt", "name", "board", "list_date", "last_trade", "close", "hfq", "adj",
    "min_low", "min_low_date", "min_close", "min_close_date",
    "issue_price", "issue_src", "issue_cmp", "first_date", "hist_cut", "warn",
]
CURRENCY = {"hk": "港元", "us": "美元"}
ISSUE_SRC_LABEL = {"em": "东方财富港股资料", "nasdaq": "纳斯达克 IPO 日历"}


def export_market(mkt: str, stocks: dict[str, GStock], meta: dict, site_dir: str = "site") -> dict:
    os.makedirs(os.path.join(site_dir, "data"), exist_ok=True)
    ready = [s for s in stocks.values() if s.has_data() and not s.excluded]
    ready.sort(key=lambda s: s.ratio())
    market_date = meta.get("market_date") or max((s.last_trade for s in ready), default="")
    rows = []
    for s in ready:
        rows.append([
            s.code, s.mkt, s.name or s.name_en, s.board, s.list_date, s.last_trade,
            _r(s.last_close, 6), _r(s.last_close, 6), 1,
            _r(s.min_low), s.min_low_date, _r(s.min_close), s.min_close_date,
            _r(s.issue_price, 6) if is_pos(s.issue_price) else None, s.issue_src,
            _r(s.issue_cmp) if is_pos(s.issue_cmp) else None, s.first_date,
            1 if hist_cut(s) else 0, s.jump_date,
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
            "代码", "名称", "英文名", "板块", f"收盘价({cur})", "数据日期",
            "历史最低价(日内最低,含发行价,按当前价格口径)", "最低价日期", "比值(现价/历史最低)", "距历史最低涨幅%",
            "历史最低收盘价(按当前价格口径)", "最低收盘日期", "比值(收盘口径,含发行价)",
            "发行价(原始)", "发行价(按当前价格口径)", "发行价来源", "发行价未采用原因", "上市日期", "数据起始日",
            "历史可能不完整", "单日涨跌超过4倍的日期",
        ])
        for s in ready:
            base_l, date_l, base_c = s.min_low, s.min_low_date, s.min_close
            if is_pos(s.issue_cmp):
                if s.issue_cmp < base_l:
                    base_l, date_l = s.issue_cmp, "发行价"
                base_c = min(base_c, s.issue_cmp)
            rl, rc = max(1.0, s.last_close / base_l), max(1.0, s.last_close / base_c)
            w.writerow([
                s.code, s.name, s.name_en, s.board, s.last_close, s.last_trade,
                round(base_l, 4), date_l, round(rl, 4), round((rl - 1) * 100, 2),
                round(s.min_close, 4), s.min_close_date, round(rc, 4),
                s.issue_price if is_pos(s.issue_price) else "", round(s.issue_cmp, 4) if is_pos(s.issue_cmp) else "",
                ISSUE_SRC_LABEL.get(s.issue_src, ""), s.issue_note, s.list_date, s.first_date,
                "是" if hist_cut(s) else "", s.jump_date,
            ])
    return {"rows": len(rows), "market_date": market_date}
