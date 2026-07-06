#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一条命令：抓取 → 分类 → 深读 → 翻译 → 生成并打开中文 HTML 报告。

去重基准是"距上次运行以来从没见过的 arxiv_id"（记录在 .daily_known_ids.json），
每次运行生成一份带时间戳 (run_id) 的独立报告，历史报告保留。

用法:
    python run_daily.py                 # 正常跑一次
    python run_daily.py --dry-run       # 只抓取+分类, 不深读/翻译
    python run_daily.py --no-open       # 生成报告但不自动打开浏览器
    python run_daily.py --no-cache      # 翻译忽略缓存重新翻
    python run_daily.py --run-id 2026-07-01_manual   # 指定 run_id
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
import translate_polymer
from daily_polymer import run_daily_scan


def main() -> int:
    ap = argparse.ArgumentParser(description="ArXiv 高分子日报: 一条命令抓取+翻译+报告")
    ap.add_argument("--run-id", default=None, help="run 标识 (默认时间戳)")
    ap.add_argument("--dry-run", action="store_true", help="只抓取+分类, 跳过深读与翻译")
    ap.add_argument("--no-open", action="store_true", help="生成后不自动打开 HTML")
    ap.add_argument("--no-cache", action="store_true", help="翻译忽略已有缓存")
    ap.add_argument("--output-dir", default=None, help="覆盖输出根目录")
    args = ap.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y-%m-%d_%H%M")
    output_root = Path(args.output_dir) if args.output_dir else Path(config.DAILY_OUTPUT_DIR)

    # ── Step A: 抓取 + 分类 (+ 深读) ──
    result = run_daily_scan(run_id=run_id, output_root=output_root, dry_run=args.dry_run)

    if result.total_fetched == 0:
        print("距上次运行无新论文，不生成报告。")
        return 0

    if args.dry_run:
        print(f"[dry-run] 完成: 抓取 {result.total_fetched} 篇, "
              f"polymer {result.polymer_count} / other {result.other_count}, "
              f"目录 {result.run_dir}")
        return 0

    if result.polymer_count == 0:
        print(f"本次有 {result.other_count} 篇其他论文，但无高分子物理论文，跳过报告生成。")
        return 0

    # ── Step B: 翻译 + 生成并打开 HTML ──
    html = translate_polymer.build_report(
        run_id=run_id,
        papers_root=str(output_root),
        use_cache=not args.no_cache,
        open_browser=not args.no_open,
    )
    if html is None:
        print("[!] 报告生成失败。")
        return 1

    print(f"\n[✓] 全部完成! 报告: {html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
