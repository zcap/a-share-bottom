from __future__ import annotations

import datetime as dt
import logging
import os
import re

BJ_TZ = dt.timezone(dt.timedelta(hours=8))
log = logging.getLogger("bottom")


def bj_now() -> dt.datetime:
    return dt.datetime.now(BJ_TZ)


def bj_today() -> str:
    return bj_now().strftime("%Y-%m-%d")


def norm_date(v) -> str:
    """把 20240105 / 2024-01-05 / 2024/1/5 / 时间戳 等统一成 YYYY-MM-DD；无法识别返回空串。"""
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})", s)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    try:
        return dt.date(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def add_days(date: str, n: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=n)).isoformat()


def summary(line: str = "") -> None:
    """写到 GitHub Actions 的运行摘要页（本地运行时只打印）。"""
    print(line, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
