#!/usr/bin/env python3
"""
Daily polymer physics paper scanner.

1. Fetch today's papers from configured arXiv categories
2. Classify each paper to find polymer-physics-related ones
3. For polymer-physics papers: deep-read and generate notes
4. Save results to the daily output directory (Obsidian vault)

Usage:
    python daily_polymer.py
    python daily_polymer.py --date 2026-06-29
    python daily_polymer.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent))

import config
from arxiv_fetcher.fetcher import ArxivFetcher
from skills.loader import load_all_skills
from agents.classifier_agent import ClassifierAgent
from agents.reader_agent import ReaderAgent
from agents.summary_agent import SummaryAgent
from paper_reader.latex_parser import parse_latex
from utils.logger import get_logger

logger = get_logger("daily-polymer")


@dataclass
class RunResult:
    """Outcome of a single daily scan run, consumed by the orchestrator."""
    run_id: str
    output_root: Path
    run_dir: Path
    total_fetched: int = 0
    polymer_count: int = 0
    other_count: int = 0


def _make_run_id() -> str:
    """Timestamped run identifier, e.g. 2026-07-01_1430."""
    return datetime.now().strftime("%Y-%m-%d_%H%M")


# ── DB helpers ───────────────────────────────────────────────

def _load_db(db_path: Path) -> dict:
    """Load the known-IDs DB. Returns {"ids": set[str], "last_run": date|None}.

    Backward compatible with the old {"processed_ids": [...]} schema that had
    no last_run field.
    """
    if not db_path.exists():
        return {"ids": set(), "last_run": None}
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"ids": set(), "last_run": None}

    ids = set(data.get("processed_ids", []))
    last_run = None
    raw = data.get("last_run")
    if raw:
        try:
            last_run = date.fromisoformat(raw)
        except ValueError:
            last_run = None
    return {"ids": ids, "last_run": last_run}


def _save_db(db_path: Path, ids: set[str], last_run: date) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "processed_ids": sorted(ids),
            "last_run": last_run.isoformat(),
            "updated": datetime.now().isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    )
    # 原子写：先写 .tmp 再 os.replace，避免写一半崩溃导致已知 ID 库损坏
    # （损坏会让下次运行重复抓取已处理过的论文）。
    tmp_path = db_path.parent / (db_path.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, db_path)


# ── Main logic ───────────────────────────────────────────────

def run_daily_scan(
    run_id: Optional[str] = None,
    output_root: Optional[Path] = None,
    dry_run: bool = False,
) -> RunResult:
    """Scan arXiv for papers new since the last run and process polymer ones.

    Returns a RunResult so the orchestrator can decide whether to translate
    and open a report.
    """

    if run_id is None:
        run_id = _make_run_id()
    if output_root is None:
        output_root = Path(config.DAILY_OUTPUT_DIR)
    run_dir = output_root / run_id
    result = RunResult(run_id=run_id, output_root=output_root, run_dir=run_dir)

    skills = load_all_skills()
    if not skills:
        raise RuntimeError("No skills loaded. Check skills/ directory.")

    # Check that polymer_physics skill exists
    if "polymer_physics" not in skills:
        logger.warning("'polymer_physics' skill not found, using 'general' fallback.")

    fetcher = ArxivFetcher()
    classifier = ClassifierAgent(skills)
    summary_agent = SummaryAgent(skills.get("general", {"reading_prompt": ""}))

    # Load known IDs + last run date to drive the fetch window
    db_path = Path(config.PROJECT_ROOT) / ".daily_known_ids.json"
    db = _load_db(db_path)
    known_ids: set[str] = db["ids"]
    last_run: Optional[date] = db["last_run"]

    # ── Step 1: Fetch papers new since the last run ──
    logger.info("=" * 60)
    logger.info(f"Step 1: Fetching papers from arXiv (run {run_id})")
    if last_run:
        logger.info(f"  Last run: {last_run} ({(date.today() - last_run).days} day(s) ago)")
    else:
        logger.info("  No previous run recorded; using default lookback window.")
    logger.info("=" * 60)

    papers = fetcher.fetch_papers(
        since=last_run,
        is_known_fn=lambda aid: aid in known_ids,
    )

    logger.info(f"Fetched {len(papers)} new papers")
    result.total_fetched = len(papers)

    if not papers:
        logger.info("No new papers since last run. Exiting.")
        # Still advance last_run so the next window starts from today.
        _save_db(db_path, known_ids, last_run=date.today())
        return result

    # ── Step 2: Classify and filter for polymer physics ──
    logger.info("=" * 60)
    logger.info("Step 2: Classifying papers")
    logger.info("=" * 60)

    polymer_papers: list[dict] = []
    other_papers: list[dict] = []

    for paper in papers:
        category = classifier.classify(paper["title"], paper.get("abstract", ""))
        paper["classified_category"] = category

        # Mark every fetched paper as known so it never reappears (cross-history dedup)
        known_ids.add(paper["arxiv_id"])

        if category == "polymer_physics":
            polymer_papers.append(paper)
            logger.info(f"  [POLYMER] {paper['arxiv_id']}: {paper['title'][:70]}")
        else:
            other_papers.append(paper)
            logger.info(f"  [{category}] {paper['arxiv_id']}: {paper['title'][:70]}")

    logger.info(f"Polymer physics papers: {len(polymer_papers)}")
    logger.info(f"Other papers: {len(other_papers)}")
    result.polymer_count = len(polymer_papers)
    result.other_count = len(other_papers)

    # ── Step 3: Deep-read polymer physics papers ──
    if polymer_papers:
        logger.info("=" * 60)
        logger.info(f"Step 3: Deep-reading {len(polymer_papers)} polymer physics papers")
        logger.info("=" * 60)

        reader = ReaderAgent("polymer_physics", skills["polymer_physics"])

        results: list[dict] = []

        for paper in polymer_papers:
            logger.info(f"  Reading: {paper['arxiv_id']} - {paper['title'][:60]}")

            if dry_run:
                notes = f"## {paper['title']}\n\n**arXiv**: {paper['arxiv_id']}\n\n**Abstract**: {paper['abstract']}\n\n*[Dry run - no deep reading]*\n"
            else:
                notes = _deep_read_or_summarize(fetcher, reader, paper, summary_agent)

            results.append({
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "abstract": paper["abstract"],
                "authors": paper.get("authors", []),
                "categories": paper.get("categories", []),
                "notes": notes,
                "published": str(paper.get("published", date.today())),
            })

        # ── Step 4: Save results ──
        logger.info("=" * 60)
        logger.info("Step 4: Saving results")
        logger.info("=" * 60)

        _save_results(results, run_id, output_root)

    # Save a quick summary of all other (non-polymer) papers — independent of
    # whether any polymer papers were found, so non-polymer days are still
    # visible in the output.
    if other_papers:
        logger.info(f"Saving summary of {len(other_papers)} other papers ...")
        _save_other_papers_summary(other_papers, run_id, output_root)

    # Persist known IDs + advance last_run to today
    _save_db(db_path, known_ids, last_run=date.today())
    logger.info(f"Saved {len(known_ids)} known IDs to {db_path}")

    # Summary
    logger.info("=" * 60)
    logger.info("Daily scan complete!")
    logger.info(f"  Run: {run_id}")
    logger.info(f"  Total fetched: {len(papers)}")
    logger.info(f"  Polymer physics: {len(polymer_papers)}")
    logger.info(f"  Other: {len(other_papers)}")
    logger.info("=" * 60)

    return result


def _deep_read_or_summarize(
    fetcher: ArxivFetcher,
    reader: ReaderAgent,
    paper: dict,
    fallback: SummaryAgent,
) -> str:
    """Attempt deep reading; fall back to summary if LaTeX source unavailable."""
    arxiv_id = paper["arxiv_id"]

    latex_source = fetcher.fetch_latex_source(arxiv_id)
    if not latex_source:
        logger.info(f"    No LaTeX source, using summary for {arxiv_id}")
        return fallback.summarize(paper)

    parsed = parse_latex(latex_source)
    if not parsed.abstract and not parsed.sections:
        logger.info(f"    LaTeX parsing empty, using summary for {arxiv_id}")
        return fallback.summarize(paper)

    return reader.read_paper(paper, parsed)


def _save_results(
    results: list[dict], run_id: str, output_root: Optional[Path] = None
) -> None:
    """Save polymer physics paper notes to the run's output directory."""
    if output_root is None:
        output_root = Path(config.DAILY_OUTPUT_DIR)

    output_root.mkdir(parents=True, exist_ok=True)

    # Save individual notes under output_root/<run_id>/
    notes_dir = output_root / run_id
    notes_dir.mkdir(parents=True, exist_ok=True)

    for r in results:
        safe_id = r["arxiv_id"].replace("/", "_")
        note_path = notes_dir / f"{safe_id}.md"
        note_content = _format_note(r)
        note_path.write_text(note_content, encoding="utf-8")
        logger.info(f"    Saved: {note_path}")

    # Save a run index
    index_path = output_root / f"{run_id}_index.md"
    index_content = _format_index(results, run_id)
    index_path.write_text(index_content, encoding="utf-8")
    logger.info(f"    Saved index: {index_path}")


def _save_other_papers_summary(
    other_papers: list[dict], run_id: str, output_root: Optional[Path] = None
) -> None:
    """Save a brief summary of non-polymer papers."""
    if output_root is None:
        output_root = Path(config.DAILY_OUTPUT_DIR)

    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / f"{run_id}_other.md"

    lines = [
        f"# Other arXiv Papers - {run_id}",
        "",
        f"**Total**: {len(other_papers)} papers (not classified as polymer physics)",
        "",
        "| arXiv ID | Title | Category |",
        "|----------|-------|----------|",
    ]

    for p in sorted(other_papers, key=lambda x: x.get("classified_category", "")):
        aid = p["arxiv_id"]
        title = p["title"][:80]
        cat = p.get("classified_category", "unknown")
        lines.append(f"| {aid} | {title} | {cat} |")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"    Saved other papers summary: {summary_path}")


def _format_note(result: dict) -> str:
    """Format a single paper note as markdown."""
    authors_str = ", ".join(result.get("authors", [])[:5])
    if len(result.get("authors", [])) > 5:
        authors_str += " et al."

    cats = ", ".join(result.get("categories", []))

    return f"""---
arxiv_id: "{result['arxiv_id']}"
title: "{result['title']}"
authors: "{authors_str}"
categories: "{cats}"
published: "{result.get('published', '')}"
processed: "{date.today().isoformat()}"
type: daily_paper
tags: [polymer-physics, daily-scan]
---

# {result['title']}

**arXiv**: [{result['arxiv_id']}]({config.ARXIV_BASE}/abs/{result['arxiv_id']})
**Authors**: {authors_str}
**Categories**: {cats}

## Abstract

{result.get('abstract', 'N/A')}

---

## Reading Notes

{result['notes']}
"""


def _format_index(results: list[dict], run_id: str) -> str:
    """Format a run index file."""
    lines = [
        f"# Daily Polymer Physics Scan - {run_id}",
        "",
        f"**Papers found**: {len(results)}",
        "",
        "## Quick Overview",
        "",
        "| # | arXiv ID | Title |",
        "|---|----------|-------|",
    ]

    for i, r in enumerate(results, 1):
        aid = r["arxiv_id"]
        title = r["title"][:100]
        safe_id = aid.replace("/", "_")
        lines.append(f"| {i} | [{aid}]({run_id}/{safe_id}.md) | {title} |")

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated: {datetime.now().isoformat()}*")

    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Daily polymer physics paper scanner"
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run identifier for the output folder (default: timestamp)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and classify only, skip deep reading",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override output root directory",
    )
    args = parser.parse_args()

    output_root = Path(args.output_dir) if args.output_dir else None

    result = run_daily_scan(
        run_id=args.run_id,
        output_root=output_root,
        dry_run=args.dry_run,
    )

    print(
        f"[scan] run={result.run_id} fetched={result.total_fetched} "
        f"polymer={result.polymer_count} other={result.other_count} "
        f"dir={result.run_dir}"
    )


if __name__ == "__main__":
    main()
