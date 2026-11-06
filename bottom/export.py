"""生成网页用的数据文件：site/data/latest.json（网页读取）和 latest.csv（可用 Excel 打开）。"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil

from .engine import Stock, board_of, is_pos

# JSON 每行的字段顺序（网页按这个顺序读取）
FIELDS = [
    "code", "mkt", "name", "board", "st", "list_date", "last_trade", "close", "hfq", "adj",
    "min_low", "min_low_date", "min_close", "min_close_date", "issue_price", "issue_src", "first_date",
]


def _r(x: float, sig: int = 7):
    """保留有效数字，减小文件体积。"""
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    if x == 0:
        return 0
    return float(f"{x:.{sig}g}")


def export(stocks: dict[str, Stock], meta: dict, site_dir: str = "site", web_dir: str = "web") -> dict:
    os.makedirs(os.path.join(site_dir, "data"), exist_ok=True)
    if os.path.isdir(web_dir):
        shutil.copytree(web_dir, site_dir, dirs_exist_ok=True)

    rows, ready = [], [s for s in stocks.values() if s.has_data()]
    market_date = max((s.last_seen for s in ready), default="")
    ready.sort(key=lambda s: s.metrics()["ratio_low"])
    for s in ready:
        rows.append([
            s.code, s.mkt, s.name, board_of(s.code, s.mkt), 1 if s.is_st else 0, s.list_date,
            s.last_trade, _r(s.last_close, 6), _r(s.close_hfq(), 9), _r(s.adj, 9), _r(s.min_low), s.min_low_date,
            _r(s.min_close), s.min_close_date, _r(s.issue_price, 6) if is_pos(s.issue_price) else None,
            s.issue_src, s.first_date,
        ])
    doc = {
        "updated_at": meta.get("updated_at", ""),
        "market_date": market_date,
        "build_pending": bool(meta.get("build_pending")),
        "total": len(stocks),
        "fields": FIELDS,
        "rows": rows,
    }
    with open(os.path.join(site_dir, "data", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    # CSV：带 BOM，Excel 直接打开不乱码
    with open(os.path.join(site_dir, "data", "latest.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "代码", "名称", "板块", "收盘价", "数据日期",
            "历史最低价(日内最低,含发行价,按当前价格口径)", "最低价日期", "比值(现价/历史最低)", "距历史最低涨幅%",
            "历史最低收盘价(含发行价,按当前价格口径)", "最低收盘日期", "比值(收盘口径)",
            "发行价", "发行价来源", "上市日期",
        ])
        for s in ready:
            m = s.metrics()
            w.writerow([
                s.code, s.name, board_of(s.code, s.mkt), s.last_close, s.last_trade,
                round(m["low_now"], 3), m["low_date"], round(m["ratio_low"], 4), round((m["ratio_low"] - 1) * 100, 2),
                round(m["close_now"], 3), m["close_date"], round(m["ratio_close"], 4),
                s.issue_price if is_pos(s.issue_price) else "",
                {"em": "东方财富", "infer": "首日前收盘推断"}.get(s.issue_src, ""), s.list_date,
            ])
    return {"rows": len(rows), "market_date": market_date}
