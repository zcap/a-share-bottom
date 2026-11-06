"""命令行入口。

python -m bottom daily     每日增量（没有历史状态时会自动先做全量构建）
python -m bottom rebuild   全量重建（每周一次，纠正可能的累计误差）
python -m bottom probe     只检查各数据源能否访问，不改动数据
python -m bottom export    只用现有状态重新生成网页数据
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

    stocks, meta = store.load()
    if mode == "export":
        info = export(stocks, meta)
        summary(f"导出 {info['rows']} 只，数据日期 {info['market_date']}")
        return 0
    if mode not in ("daily", "rebuild"):
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


if __name__ == "__main__":
    sys.exit(main(sys.argv))
