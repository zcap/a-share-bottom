"""生成网页用的数据文件：site/data/latest.json（网页读取）和 latest.csv（可用 Excel 打开）。"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil

from .engine import Stock, board_of, compute_scores, is_pos, metric_value

# JSON 每行的字段顺序（网页按这个顺序读取）
FIELDS = [
    "code", "mkt", "name", "board", "st", "list_date", "last_trade", "close", "hfq", "adj",
    "min_low", "min_low_date", "min_close", "min_close_date", "issue_price", "issue_src", "first_date",
    "pe", "pb", "ps", "val_date", "vscore",
    "industry", "roe", "gpm", "cfroe", "rev_yoy", "np_yoy", "fin_period", "qscore", "gscore", "tscore",
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
    scores = compute_scores(ready)

    def sc(code, k):
        v = scores.get(code, {}).get(k)
        return round(v, 1) if v is not None else None
    for s in ready:
        rows.append([
            s.code, s.mkt, s.name, board_of(s.code, s.mkt), 1 if s.is_st else 0, s.list_date,
            s.last_trade, _r(s.last_close, 6), _r(s.close_hfq(), 9), _r(s.adj, 9), _r(s.min_low), s.min_low_date,
            _r(s.min_close), s.min_close_date, _r(s.issue_price, 6) if is_pos(s.issue_price) else None,
            s.issue_src, s.first_date,
            _r(s.pe, 5), _r(s.pb, 5), _r(s.ps, 5), s.val_date,
            sc(s.code, "value"),
            s.industry, _r(s.roe, 4), _r(s.gpm, 4), _r(metric_value(s, "cfroe") * 100, 4),
            _r(s.rev_yoy, 4), _r(s.np_yoy, 4), s.fin_period,
            sc(s.code, "quality"), sc(s.code, "growth"), sc(s.code, "total"),
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
            "综合分(价值50%+质量30%+成长20%)", "价值分(同行业内,越高越便宜)", "质量分", "成长分", "行业",
            "市盈率TTM", "市净率MRQ", "市销率TTM", "估值日期",
            "净资产收益率%", "销售毛利率%", "经营现金流/净资产%", "营收同比%", "净利润同比%", "财报期",
        ])
        for s in ready:
            m = s.metrics()
            w.writerow([
                s.code, s.name, board_of(s.code, s.mkt), s.last_close, s.last_trade,
                round(m["low_now"], 3), m["low_date"], round(m["ratio_low"], 4), round((m["ratio_low"] - 1) * 100, 2),
                round(m["close_now"], 3), m["close_date"], round(m["ratio_close"], 4),
                s.issue_price if is_pos(s.issue_price) else "",
                {"em": "东方财富", "infer": "首日前收盘推断"}.get(s.issue_src, ""), s.list_date,
                *(("" if sc(s.code, k) is None else sc(s.code, k)) for k in ("total", "value", "quality", "growth")),
                s.industry,
                *(round(v, 2) if isinstance(v, float) and math.isfinite(v) else "" for v in (s.pe, s.pb, s.ps)),
                s.val_date,
                *(round(v, 2) if isinstance(v, float) and math.isfinite(v) else ""
                  for v in (s.roe, s.gpm, metric_value(s, "cfroe") * 100, s.rev_yoy, s.np_yoy)),
                s.fin_period,
            ])
    return {"rows": len(rows), "market_date": market_date}
