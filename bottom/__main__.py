"""命令行入口。

python -m bottom daily     每日增量（没有历史状态时会自动先做全量构建）
python -m bottom verify    每周核对：只核对复权因子，对不上的才重算（约 1 小时）
python -m bottom rebuild   全量重建（每月一次，约 6 小时，分两次运行完成）
python -m bottom probe     只检查各数据源能否访问，不改动数据
python -m bottom probe-global  检查港股、美股及各市场发行价数据源（需先安装 requirements-global.txt）
python -m bottom export    只用现有状态重新生成网页数据
python -m bottom hk [daily|full]   港股：每日更新（full = 强制重新下载并刷新发行价资料）
python -m bottom us [daily|full]   美股：同上
python -m bottom site      用三个市场的状态（state、state-hk、state-us 目录）生成整个网页
"""

from __future__ import annotations

import logging
import os
import sys
import time


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    mode = (argv[1] if len(argv) > 1 else "daily").strip().lower()

    from . import store
    from .export import export
    from .util import summary

    if mode == "probe":
        from .probe import probe

        return probe()
    if mode == "probe-global":
        from .probe_global import probe_global

        return probe_global()

    if mode == "site":
        return build_site()
    if mode in ("hk", "us"):
        return run_global(mode, (argv[2] if len(argv) > 2 else "daily").strip().lower())

    stocks, meta = store.load()
    if mode == "export":
        info = export(stocks, meta)
        summary(f"导出 {info['rows']} 只，数据日期 {info['market_date']}")
        return 0
    if mode not in ("daily", "rebuild", "verify"):
        print(__doc__)
        return 2

    from .pipeline import Sources, Updater, report

    budget_min = float(os.environ.get("TIME_BUDGET_MIN", "320"))
    src = Sources()
    up = Updater(src, stocks, meta, deadline=time.time() + budget_min * 60)
    up.save_cb = store.save
    try:
        up.run(mode)
    finally:
        # 无论成功与否都保存进度，下次接着做
        store.save(up.stocks, up.meta)
        src.close()
    info = export(up.stocks, up.meta)
    report(up)
    summary(f"- 网页数据：{info['rows']} 只，数据日期 {info['market_date']}")
    return 0


def run_global(mkt: str, sub: str) -> int:
    from . import store
    from .gengine import GStock
    from .gexport import export_market
    from .gpipeline import GlobalSources, GlobalUpdater, report
    from .util import summary

    if sub not in ("daily", "full"):
        print(__doc__)
        return 2
    budget_min = float(os.environ.get("TIME_BUDGET_MIN", "100"))
    stocks, meta = store.load(cls=GStock)
    up = GlobalUpdater(mkt, GlobalSources(mkt), stocks, meta, state_dir=store.STATE_DIR,
                       deadline=time.time() + budget_min * 60, workers=int(os.environ.get("YF_WORKERS", "8")))
    try:
        up.run(sub)
    finally:
        store.save(up.stocks, up.meta)
    info = export_market(mkt, up.stocks, up.meta)
    report(up)
    summary(f"- 网页数据：{info['rows']} 只，数据日期 {info['market_date']}")
    return 0


def build_site() -> int:
    """发布网页前，用三个市场各自的状态生成全部数据文件（只用标准库，不联网）。"""
    import json

    from . import store
    from .export import export
    from .gengine import GStock
    from .gexport import export_market
    from .util import summary

    stocks, meta = store.load(os.environ.get("STATE_DIR_A", "state"))
    info = export(stocks, meta)  # 同时把 web/ 复制到 site/
    markets = {"a": {**info, "updated_at": meta.get("updated_at", "")}}
    for mkt in ("hk", "us"):
        st, mt = store.load(os.environ.get(f"STATE_DIR_{mkt.upper()}", f"state-{mkt}"), cls=GStock)
        markets[mkt] = {**export_market(mkt, st, mt), "updated_at": mt.get("updated_at", "")}
    with open(os.path.join("site", "data", "markets.json"), "w", encoding="utf-8") as f:
        json.dump(markets, f, ensure_ascii=False)
    for k, v in markets.items():
        summary(f"- {dict(a='A股', hk='港股', us='美股')[k]}：{v['rows']} 只，数据日期 {v['market_date'] or '（还没有数据）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
