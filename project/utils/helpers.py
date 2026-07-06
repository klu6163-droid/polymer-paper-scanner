"""Utility helpers for text processing, truncation, etc."""

import re
from datetime import date, timedelta


def sanitize_filename(title: str, max_len: int = 80) -> str:
    """Convert a paper title into a safe filename slug."""
    safe = re.sub(r"[^\w\s-]", "", title)
    safe = re.sub(r"\s+", "-", safe).strip("-")
    return safe[:max_len]


def truncate_text(text: str, max_chars: int = 60000) -> str:
    """Truncate text to fit within a character budget, keeping the beginning."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n...[内容因超出长度限制被截断]..."


def get_target_dates() -> list[date]:
    """Return today and yesterday as candidate dates."""
    today = date.today()
    return [today, today - timedelta(days=1)]


def extract_arxiv_id(entry_id: str) -> str:
    """Extract a clean arxiv id from a URL or bare id string.

    Handles:
      - new-style URLs:  https://arxiv.org/abs/2401.12345v2      -> 2401.12345
      - old-style URLs:  https://arxiv.org/abs/cond-mat/0501001v2 -> cond-mat/0501001
      - pdf URLs:        https://arxiv.org/pdf/2401.12345.pdf    -> 2401.12345
      - bare ids:        2401.12345v1                            -> 2401.12345
    """
    if not entry_id:
        return ""
    s = entry_id.strip()
    # Prefer the segment after /abs/ or /pdf/ (preserves old-style archive/YYMMNNN)
    m = re.search(r"/(?:abs|pdf)/([^?#]+)", s)
    raw = m.group(1) if m else s.rstrip("/").split("/")[-1]
    raw = re.sub(r"\.pdf$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"v\d+$", "", raw)
    return raw.strip()
