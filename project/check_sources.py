#!/usr/bin/env python3
"""
源健康自检 —— 真打每个数据源(不 mock)，报 OK/EMPTY/ERROR + 条数 + 字段覆盖率。

任一主源 ERROR 或 EMPTY 退出码非 0，可挂 cron 在静默失效时报警
(arXiv 改 RSS 字段 / 加反爬时，fixture 单测全绿但生产已挂)。

失败判定:
  - RSS / arXiv API:  ERROR / EMPTY / PARTIAL 都算失败(主源)
  - e-print:          ERROR 算失败；EMPTY 仅警告(单篇可能本就无 LaTeX 源)
  - OpenAlex:         ERROR 算失败；EMPTY/SKIP 不算(补充源，默认关闭)

用法:
    python check_sources.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

import config
from arxiv_fetcher.fetcher import ArxivFetcher
from utils.logger import get_logger

logger = get_logger("check-sources")

FIELD_KEYS = ("title", "abstract", "authors", "published")

# 主源：EMPTY 也是失败。补充源：只有 ERROR 算失败。
HARD_SOURCES = {"RSS (4 cats)", "arXiv API"}


def _coverage(papers: list[dict]) -> dict[str, float]:
    """Fraction of papers with a non-empty value for each key."""
    if not papers:
        return {k: 0.0 for k in FIELD_KEYS}
    return {
        k: round(sum(1 for p in papers if p.get(k)) / len(papers), 2)
        for k in FIELD_KEYS
    }


def _fmt_coverage(cov: dict[str, float]) -> str:
    return "  ".join(f"{k[:4]}={v:.2f}" for k, v in cov.items())


def probe_rss(fetcher: ArxivFetcher) -> tuple[str, int, str, list[dict]]:
    """Probe each RSS category individually so a partial failure (some
    feeds erroring after retries) isn't masked by the feeds that still
    return papers. Uses the real _fetch_with_retry / _parse_rss primitives.
    """
    all_papers: list[dict] = []
    per_cat: list[str] = []
    n_ok = 0
    try:
        for cat in fetcher.categories:
            url = f"{config.ARXIV_RSS_BASE}/rss/{cat}"
            resp = fetcher._fetch_with_retry(url)
            if resp is None:
                per_cat.append(f"{cat}:FAIL")
                continue
            papers = fetcher._parse_rss(resp.text, cat)
            all_papers.extend(papers)
            per_cat.append(f"{cat}:OK({len(papers)})")
            n_ok += 1  # feed responded → healthy (0 new today is legit)
    except Exception as e:
        return "ERROR", 0, str(e), []

    all_papers = fetcher._dedup_papers(all_papers)
    total = len(fetcher.categories)
    note = " ".join(per_cat) + f" | unique={len(all_papers)}"

    if n_ok == 0:
        status = "EMPTY"
    elif n_ok < total:
        status = "PARTIAL"  # some feeds failed after retries → alert
    else:
        status = "OK" if all_papers else "EMPTY"
    return status, len(all_papers), note, all_papers


def probe_api(fetcher: ArxivFetcher) -> tuple[str, int, str, list[dict]]:
    try:
        # 7-day window so EMPTY is a real signal, not "no papers today"
        cutoff = date.today() - timedelta(days=7)
        papers = fetcher._fetch_via_api(cutoff_date=cutoff, is_known_fn=None)
        if not papers:
            return "EMPTY", 0, "no papers in last 7 days", []
        return "OK", len(papers), _fmt_coverage(_coverage(papers)), papers
    except Exception as e:
        return "ERROR", 0, str(e), []


def probe_eprint(
    fetcher: ArxivFetcher, arxiv_id: Optional[str]
) -> tuple[str, int, str]:
    if not arxiv_id:
        return "SKIP", 0, "no sample id (RSS/API both empty?)"
    try:
        src = fetcher.fetch_latex_source(arxiv_id)
        if not src:
            return "EMPTY", 0, f"no latex for {arxiv_id}"
        # sanity: real LaTeX source contains at least one of these markers
        looks_like_tex = (
            "\\begin{document}" in src or "\\section" in src or "\\title" in src
        )
        status = "OK" if looks_like_tex else "EMPTY"
        return status, len(src), f"{arxiv_id} ({len(src)} chars, tex={looks_like_tex})"
    except Exception as e:
        return "ERROR", 0, str(e)


def probe_openalex(
    fetcher: ArxivFetcher, arxiv_id: Optional[str]
) -> tuple[str, int, str]:
    if not config.OPENALEX_ENABLED:
        return "SKIP", 0, "OPENALEX_ENABLED=0"
    if not arxiv_id:
        return "SKIP", 0, "no sample id"
    try:
        data = fetcher._query_openalex(arxiv_id)
        if not data:
            return "EMPTY", 0, f"no record for {arxiv_id}"
        return "OK", 1, f"cited_by={data.get('cited_by_count', '?')}"
    except Exception as e:
        return "ERROR", 0, str(e)


def _pick_sample_id(*paper_lists: list[dict]) -> Optional[str]:
    """First non-empty arxiv_id across the supplied paper lists."""
    for papers in paper_lists:
        for p in papers:
            if p.get("arxiv_id"):
                return p["arxiv_id"]
    return None


def main() -> int:
    fetcher = ArxivFetcher()

    print("=" * 72)
    print("Source health check")
    print("=" * 72)

    rows: list[tuple[str, str, int, str]] = []

    rss_status, rss_n, rss_note, rss_papers = probe_rss(fetcher)
    rows.append(("RSS (4 cats)", rss_status, rss_n, rss_note))
    print(f"  RSS (4 cats) ...... {rss_status:6} n={rss_n:<5} {rss_note}")

    api_status, api_n, api_note, api_papers = probe_api(fetcher)
    rows.append(("arXiv API", api_status, api_n, api_note))
    print(f"  arXiv API ......... {api_status:6} n={api_n:<5} {api_note}")

    # Reuse the already-fetched papers to pick a real recent id for the
    # e-print + OpenAlex probes (avoids both a second fetch and hardcoding
    # a "stable" id that could itself rot).
    sample_id = _pick_sample_id(rss_papers, api_papers)

    eprint_status, eprint_n, eprint_note = probe_eprint(fetcher, sample_id)
    rows.append(("e-print (LaTeX)", eprint_status, eprint_n, eprint_note))
    print(f"  e-print (LaTeX) ... {eprint_status:6} n={eprint_n:<5} {eprint_note}")

    oa_status, oa_n, oa_note = probe_openalex(fetcher, sample_id)
    rows.append(("OpenAlex", oa_status, oa_n, oa_note))
    print(f"  OpenAlex .......... {oa_status:6} n={oa_n:<5} {oa_note}")

    print("=" * 72)

    failed = [
        name
        for name, status, _, _ in rows
        if status == "ERROR"
        or (status in ("EMPTY", "PARTIAL") and name in HARD_SOURCES)
    ]
    if failed:
        print(f"[FAIL] {len(failed)} source(s) failed: {', '.join(failed)}")
        return 1
    print("[OK] all sources healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
