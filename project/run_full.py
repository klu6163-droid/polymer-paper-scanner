#!/usr/bin/env python3
"""
一键包装: 扫描 + 翻译 + WebUI（同进程顺序执行）。

逻辑已合并入 run_daily.py；本文件保留为兼容入口，供已有定时任务调用。
新增功能（--dry-run / --no-open / --no-cache / --run-id 等）请直接用 run_daily.py。

用法:
    python run_full.py              # 等价于 python run_daily.py
    python run_full.py --no-open    # 透传给 run_daily
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_daily

if __name__ == "__main__":
    sys.exit(run_daily.main())
