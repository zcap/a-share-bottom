"""计算状态的读写。状态保存在 state/ 目录（GitHub 上存放在 data 分支）。"""

from __future__ import annotations

import csv
import gzip
import json
import os

from .engine import Stock

STATE_DIR = os.environ.get("STATE_DIR", "state")
STOCKS_FILE = "stocks.csv.gz"
META_FILE = "meta.json"
VERSION = 1


def load(state_dir: str = STATE_DIR, cls=Stock) -> tuple[dict, dict]:
    """读取状态。cls 是每行的类型（A 股 Stock，港美股 GStock）。"""
    stocks: dict = {}
    meta: dict = {}
    mp = os.path.join(state_dir, META_FILE)
    sp = os.path.join(state_dir, STOCKS_FILE)
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    if meta.get("version") != VERSION:
        return {}, {"version": VERSION}
    if os.path.exists(sp):
        with gzip.open(sp, "rt", encoding="utf-8", newline="") as f:
            for rec in csv.DictReader(f):
                s = cls.from_record(rec)
                stocks[s.code] = s
    return stocks, meta


def save(stocks: dict, meta: dict, state_dir: str = STATE_DIR) -> None:
    os.makedirs(state_dir, exist_ok=True)
    meta = {**meta, "version": VERSION}
    sp = os.path.join(state_dir, STOCKS_FILE)
    tmp = sp + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        cls = type(next(iter(stocks.values()))) if stocks else Stock
        w = csv.DictWriter(f, fieldnames=cls.columns())
        w.writeheader()
        for code in sorted(stocks):
            w.writerow(stocks[code].to_record())
    os.replace(tmp, sp)
    with open(os.path.join(state_dir, META_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, sort_keys=True)
