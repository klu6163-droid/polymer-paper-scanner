"""
ArXiv Fetcher — fetch recent papers and their LaTeX source.

Strategy (in order):
  1. RSS feed   – fast, gives today's new listings directly
  2. arxiv API  – fallback, get the most recent N papers (no date filter)

Uses direct HTTP download for LaTeX source files.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote, urlencode

import arxiv
import requests

import config
from utils.helpers import extract_arxiv_id
from utils.logger import get_logger

logger = get_logger(__name__)

# arXiv RSS namespaces
_RSS_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "taxo": "http://purl.org/rss/1.0/modules/taxonomy/",
}


class ArxivFetcher:
    """Fetch paper listings and LaTeX source from arXiv."""

    def __init__(self):
        self.client = arxiv.Client(
            page_size=100,
            delay_seconds=3.0,
            num_retries=3,
        )
        self.categories = config.ARXIV_CATEGORIES
        self.max_results = config.ARXIV_MAX_RESULTS

        # Shared session for connection pooling + consistent headers
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "PolymerPaperScanner/1.0 (https://github.com/arxiv-polymer)"
        })

    # ── Paper listing ─────────────────────────────────────────

    def fetch_single_paper(self, arxiv_id_or_url: str) -> Optional[dict]:
        """
        Fetch a single paper by arxiv_id or URL.
        
        Parameters
        ----------
        arxiv_id_or_url : str
            Can be:
            - arxiv_id: "2401.12345"
            - abs URL: "https://arxiv.org/abs/2401.12345"
            - pdf URL: "https://arxiv.org/pdf/2401.12345.pdf"
        
        Returns
        -------
        dict or None
            Paper metadata dict, or None if not found.
        """
        # Extract arxiv_id from URL if needed
        arxiv_id = extract_arxiv_id(arxiv_id_or_url)
        if not arxiv_id:
            logger.error(f"Invalid arxiv_id or URL: {arxiv_id_or_url}")
            return None
        
        logger.info(f"Fetching single paper: {arxiv_id}")
        
        try:
            search = arxiv.Search(id_list=[arxiv_id])
            results = list(self.client.results(search))
            
            if not results:
                logger.error(f"Paper not found: {arxiv_id}")
                return None
            
            paper = self._result_to_dict(results[0])
            logger.info(f"  Found: {paper['title'][:60]}...")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to fetch {arxiv_id}: {e}")
            return None

    def fetch_papers(
        self,
        since: Optional[date] = None,
        lookback_days: Optional[int] = None,
        is_known_fn: Optional[Callable[[str], bool]] = None,
        target_date: Optional[date] = None,  # deprecated alias, unused
    ) -> list[dict]:
        """
        Fetch recent papers from arXiv.

        Strategy depends on how far back we need to look:
        - lookback <= 1 day: RSS feeds (today's new listings), fall back to
          the arxiv API if RSS yields nothing.
        - lookback  > 1 day: go straight to the arxiv API, which can backfill
          the gap since the last run (RSS only ever exposes *today's* listing).

        Stop conditions (both RSS and API):
          - Paper already in DB (is_known_fn returns True) → filter out / stop
          - Paper older than the cutoff date → stop

        Parameters
        ----------
        since : date, optional
            The date of the last run. Used to compute the lookback window so
            that a multi-day gap gets backfilled. If None, falls back to
            config.FETCH_LOOKBACK_DAYS.
        lookback_days : int, optional
            Explicit lookback window; overrides `since` when provided.
        is_known_fn : callable, optional
            Function that takes an arxiv_id and returns True if the
            paper is already tracked in the local DB.
        """
        # ── Resolve the lookback window ──
        if lookback_days is not None:
            lb = lookback_days
        elif since is not None:
            lb = (date.today() - since).days
        else:
            lb = config.FETCH_LOOKBACK_DAYS
        # Clamp: at least 1 day, at most 30 days (avoid runaway backfill).
        lb = max(1, min(lb, 30))
        cutoff_date = date.today() - timedelta(days=lb)
        logger.info(f"Fetch window: lookback={lb} day(s), cutoff={cutoff_date}")

        # ── Multi-day gap → API backfill (RSS can't backfill) ──
        if lb > 1:
            logger.info(
                "Lookback > 1 day, using arxiv API to backfill the gap ..."
            )
            return self._fetch_via_api(
                cutoff_date=cutoff_date, is_known_fn=is_known_fn
            )

        # ── Method 1: RSS feeds (daily case) ──
        logger.info(
            f"Fetching papers via RSS from [{', '.join(self.categories)}] ..."
        )
        papers = self._fetch_via_rss()
        if papers:
            # Dedup by arxiv_id (a paper can appear in multiple category feeds)
            papers = self._dedup_papers(papers)
            logger.info(f"RSS: got {len(papers)} unique papers")

            # Filter out papers already in DB
            if is_known_fn:
                before = len(papers)
                papers = [p for p in papers if not is_known_fn(p["arxiv_id"])]
                skipped = before - len(papers)
                if skipped:
                    logger.info(
                        f"  RSS: filtered out {skipped} papers already in DB, "
                        f"{len(papers)} new papers remaining"
                    )

            if self.max_results and len(papers) > self.max_results:
                papers = papers[: self.max_results]
            return papers

        # ── Method 2: arxiv API (fallback) ──
        logger.info("RSS returned nothing, falling back to arxiv API ...")
        api_papers = self._fetch_via_api(
            cutoff_date=cutoff_date, is_known_fn=is_known_fn
        )

        if api_papers:
            return api_papers

        # ── Method 3: OpenAlex (last resort) ──
        if config.OPENALEX_ENABLED:
            logger.info("arXiv API also returned nothing, trying OpenAlex ...")
            return self.fetch_via_openalex(
                cutoff_date=cutoff_date, is_known_fn=is_known_fn
            )

        return []

    # ── RSS fetching ──────────────────────────────────────────

    def _fetch_with_retry(
        self,
        url: str,
        timeout: int = None,
        max_retries: int = None,
    ) -> Optional[requests.Response]:
        """GET with exponential backoff. Returns Response or None."""
        timeout = timeout or config.RSS_TIMEOUT
        max_retries = max_retries or config.RSS_MAX_RETRIES
        for attempt in range(max_retries):
            try:
                resp = self._session.get(url, timeout=timeout)
                resp.raise_for_status()
                return resp
            except Exception as e:
                wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
                logger.warning(
                    f"  Request attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
        logger.error(f"  Exhausted {max_retries} retries for {url}")
        return None

    def _fetch_via_rss(self) -> list[dict]:
        """Fetch today's new papers from arXiv RSS feeds (parallel + retry)."""
        if config.RSS_PARALLEL and len(self.categories) > 1:
            return self._fetch_via_rss_parallel()
        return self._fetch_via_rss_serial()

    def _fetch_via_rss_serial(self) -> list[dict]:
        """Serial RSS fetch with retry."""
        all_papers: list[dict] = []
        for cat in self.categories:
            url = f"{config.ARXIV_RSS_BASE}/rss/{cat}"
            logger.info(f"  RSS: {url}")
            resp = self._fetch_with_retry(url)
            if resp is None:
                continue
            papers = self._parse_rss(resp.text, cat)
            all_papers.extend(papers)
            logger.info(f"    -> {len(papers)} papers from {cat}")
        return all_papers

    def _fetch_via_rss_parallel(self) -> list[dict]:
        """Parallel RSS fetch: 4 categories concurrently."""
        all_papers: list[dict] = []

        def _fetch_one(cat: str) -> list[dict]:
            url = f"{config.ARXIV_RSS_BASE}/rss/{cat}"
            logger.info(f"  RSS: {url}")
            resp = self._fetch_with_retry(url)
            if resp is None:
                return []
            papers = self._parse_rss(resp.text, cat)
            logger.info(f"    -> {len(papers)} papers from {cat}")
            return papers

        with ThreadPoolExecutor(max_workers=len(self.categories)) as pool:
            futures = {pool.submit(_fetch_one, cat): cat for cat in self.categories}
            for fut in as_completed(futures):
                try:
                    all_papers.extend(fut.result())
                except Exception as e:
                    logger.warning(f"  RSS thread error for {futures[fut]}: {e}")
        return all_papers

    def _parse_rss(self, xml_text: str, category: str) -> list[dict]:
        """Parse an arXiv RSS feed XML into paper dicts."""
        papers: list[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"  RSS XML parse error: {e}")
            return papers

        # arXiv RSS uses RDF/RSS 1.0 format
        items = root.findall(".//rss:item", _RSS_NS)
        if not items:
            # Try plain RSS 2.0 fallback
            items = root.findall(".//item")

        for item in items:
            paper = self._rss_item_to_dict(item, category)
            if paper:
                papers.append(paper)

        return papers

    def _rss_item_to_dict(self, item: ET.Element, default_cat: str) -> Optional[dict]:
        """Convert a single RSS <item> to our paper dict."""
        # NOTE: must use explicit `is None` checks here, NOT `find(...) or
        # find(...)`. ElementTree Elements with only text (no child elements)
        # are falsy, so `a or b` would silently skip a valid `a` and fall
        # through to b — which previously made every RSS item parse as None.
        title_el = item.find("rss:title", _RSS_NS)
        if title_el is None:
            title_el = item.find("title")
        link_el = item.find("rss:link", _RSS_NS)
        if link_el is None:
            link_el = item.find("link")
        desc_el = item.find("rss:description", _RSS_NS)
        if desc_el is None:
            desc_el = item.find("description")
        creator_el = item.find("dc:creator", _RSS_NS)

        if title_el is None or link_el is None:
            return None

        raw_title = (title_el.text or "").strip()
        link = (link_el.text or "").strip()
        raw_desc = (desc_el.text or "").strip() if desc_el is not None else ""

        # Skip "UPDATED" entries, focus on new submissions
        # arXiv RSS titles look like: "Title (arXiv:2401.12345v1 [cs.AI])"
        # or sometimes: "Title. (arXiv:2401.12345v1 [cs.AI] UPDATED)"
        is_updated = "UPDATED" in raw_title

        # Extract arxiv ID from link: https://arxiv.org/abs/2401.12345
        arxiv_id = extract_arxiv_id(link) if link else ""
        if not arxiv_id:
            return None

        # Clean title: remove the trailing "(arXiv:...)" part
        title = re.sub(r"\s*\(arXiv:[^)]+\)\s*$", "", raw_title).strip()
        title = re.sub(r"\.\s*$", "", title)  # remove trailing period

        # Parse abstract from description (may contain HTML)
        abstract = self._clean_html(raw_desc)
        # arXiv RSS description is typically:
        #   "arXiv:ID Announce Type: ...\nAbstract: <actual abstract>"
        # Strip everything up to and including "Abstract:" so saved notes
        # don't retain the announce-type prefix noise.
        abs_match = re.search(r"Abstract:\s*", abstract, flags=re.IGNORECASE)
        if abs_match:
            abstract = abstract[abs_match.end():]
        abstract = abstract.strip()

        # Parse authors
        authors: list[str] = []
        if creator_el is not None and creator_el.text:
            # Format: "<a href='...'>Author1</a>, <a href='...'>Author2</a>"
            author_text = self._clean_html(creator_el.text)
            authors = [a.strip() for a in author_text.split(",") if a.strip()]

        # Parse publication date from dc:date (ISO 8601, e.g.
        # "2026-06-30" or "2026-06-30T00:00:00Z"). Fall back to today
        # only if the element is missing or unparseable.
        published = date.today()
        date_el = item.find("dc:date", _RSS_NS)
        if date_el is not None and date_el.text:
            try:
                published = date.fromisoformat(date_el.text.strip()[:10])
            except ValueError:
                pass

        # Extract categories from title bracket part
        cat_match = re.search(r"\[([^\]]+)\]", raw_title)
        categories = (
            [c.strip() for c in cat_match.group(1).split(",")]
            if cat_match
            else [default_cat]
        )

        return {
            "arxiv_id": arxiv_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "categories": categories,
            "published": published,
            "pdf_url": f"{config.ARXIV_BASE}/pdf/{arxiv_id}",
            "is_updated": is_updated,
        }

    # ── API fallback ──────────────────────────────────────────

    def _fetch_via_api(
        self,
        cutoff_date: Optional[date] = None,
        is_known_fn: Optional[Callable[[str], bool]] = None,
    ) -> list[dict]:
        """
        Fallback / backfill: fetch recent papers via the arxiv API.

        Papers are sorted newest-first. Early-stop when:
          1. Paper already in local DB → all older papers should also be known
          2. Paper published before the cutoff date → out of the window

        Parameters
        ----------
        cutoff_date : date, optional
            Oldest publication date to include. Papers strictly older than
            this are dropped and iteration stops. Defaults to
            (today - config.FETCH_LOOKBACK_DAYS).
        """
        cat_query = " OR ".join(f"cat:{cat}" for cat in self.categories)
        search = arxiv.Search(
            query=cat_query,
            max_results=self.max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )

        if cutoff_date is None:
            cutoff_date = date.today() - timedelta(days=config.FETCH_LOOKBACK_DAYS)

        papers: list[dict] = []
        total_scanned = 0
        logger.info(
            f"  API: fetching up to {self.max_results} papers "
            f"(cutoff date: {cutoff_date}, stop on first known paper) ..."
        )

        for result in self.client.results(search):
            total_scanned += 1
            paper = self._result_to_dict(result)

            # ── Early stop 1: paper older than yesterday ──
            if paper["published"] < cutoff_date:
                logger.info(
                    f"  ■ Stop: reached paper from {paper['published']} "
                    f"(cutoff {cutoff_date}). Scanned {total_scanned}."
                )
                break

            # ── Early stop 2: paper already in DB → we've fetched up to here ──
            if is_known_fn and is_known_fn(paper["arxiv_id"]):
                logger.info(
                    f"  ■ Stop: paper {paper['arxiv_id']} already in DB. "
                    f"All older papers should be known too. Scanned {total_scanned}."
                )
                break

            papers.append(paper)
            if len(papers) >= self.max_results:
                break

        logger.info(f"  API: got {len(papers)} new papers (scanned {total_scanned})")
        return papers

    # ── LaTeX source fetching ─────────────────────────────────

    def fetch_latex_source(self, arxiv_id: str) -> Optional[str]:
        """
        Fetch the LaTeX source of a paper.

        1. Try `arxiv_to_prompt` library (if installed).
        2. Fall back to manual e-print download with timeout + retry.
        """
        # ── Method 1: arxiv_to_prompt ──
        try:
            from arxiv_to_prompt import process_latex_source

            logger.info(f"  Fetching source via arxiv_to_prompt: {arxiv_id}")
            return process_latex_source(arxiv_id, keep_comments=False)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"  arxiv_to_prompt failed for {arxiv_id}: {e}")

        # ── Method 2: manual e-print download with retry ──
        logger.info(f"  Falling back to manual e-print: {arxiv_id}")
        return self._fetch_eprint_manual(arxiv_id)

    def _fetch_eprint_manual(self, arxiv_id: str) -> Optional[str]:
        """Download e-print tar.gz from arXiv with timeout + retry."""
        url = f"{config.ARXIV_BASE}/e-print/{arxiv_id}"
        max_retries = config.EPRINT_MAX_RETRIES
        timeout = config.EPRINT_TIMEOUT

        for attempt in range(max_retries):
            try:
                resp = self._session.get(url, timeout=timeout)
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "")
                data = resp.content

                # arXiv e-print can be tar.gz or plain gzip
                if "x-tar" in content_type or tarfile.is_tarfile(io.BytesIO(data)):
                    return self._extract_tex_from_tar(data)
                elif content_type == "application/gzip" or data[:2] == b"\x1f\x8b":
                    # Plain gzip (single .tex file)
                    try:
                        decompressed = gzip.decompress(data)
                        return decompressed.decode("utf-8", errors="ignore")
                    except Exception:
                        pass

                # Unknown format, try as tar anyway
                try:
                    return self._extract_tex_from_tar(data)
                except Exception:
                    logger.warning(
                        f"  e-print unknown format for {arxiv_id} "
                        f"(Content-Type: {content_type})"
                    )
                    return None

            except requests.Timeout:
                logger.warning(
                    f"  e-print timeout for {arxiv_id} "
                    f"(attempt {attempt + 1}/{max_retries}, {timeout}s)"
                )
            except Exception as e:
                logger.warning(
                    f"  e-print attempt {attempt + 1}/{max_retries} "
                    f"failed for {arxiv_id}: {e}"
                )

            if attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                logger.info(f"    Retrying in {wait}s ...")
                time.sleep(wait)

        logger.error(f"  e-print exhausted {max_retries} retries for {arxiv_id}")
        return None

    @staticmethod
    def _extract_tex_from_tar(data: bytes) -> Optional[str]:
        """Extract main .tex content from a tar archive (bytes)."""
        tar = tarfile.open(fileobj=io.BytesIO(data))
        try:
            return ArxivFetcher._find_main_tex(tar)
        finally:
            tar.close()

    @staticmethod
    def _find_main_tex(tar: tarfile.TarFile) -> Optional[str]:
        """Find the main .tex file in a tar archive."""
        tex_files: list[tuple[str, str]] = []

        for member in tar.getmembers():
            if member.name.endswith(".tex") and not member.name.startswith("."):
                f = tar.extractfile(member)
                if f:
                    content = f.read().decode("utf-8", errors="ignore")
                    tex_files.append((member.name, content))

        if not tex_files:
            return None

        # Prefer the file containing \begin{document}
        for name, content in tex_files:
            if "\\begin{document}" in content:
                return content

        # Fall back to largest tex file
        tex_files.sort(key=lambda x: len(x[1]), reverse=True)
        return tex_files[0][1]

    @staticmethod
    def _result_to_dict(result: arxiv.Result) -> dict:
        """Convert an arxiv.Result to a plain dict."""
        return {
            "arxiv_id": extract_arxiv_id(result.entry_id),
            "title": result.title.replace("\n", " ").strip(),
            "abstract": result.summary.replace("\n", " ").strip(),
            "authors": [a.name for a in result.authors],
            "categories": list(result.categories),
            "published": result.published.date(),
            "pdf_url": result.pdf_url or "",
        }

    @staticmethod
    def _dedup_papers(papers: list[dict]) -> list[dict]:
        """Remove duplicate papers by arxiv_id, keeping the first."""
        seen: set[str] = set()
        unique: list[dict] = []
        for p in papers:
            if p["arxiv_id"] not in seen:
                seen.add(p["arxiv_id"])
                unique.append(p)
        return unique

    @staticmethod
    def _clean_html(text: str) -> str:
        """Strip HTML tags from text."""
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'")
        return text.strip()

    # ── OpenAlex supplementary source ─────────────────────────

    def enrich_with_openalex(self, papers: list[dict]) -> list[dict]:
        """
        Enrich paper dicts with OpenAlex metadata (citation count, concepts).

        Uses the OpenAlex API which is free (no key, 100 req/s).
        Only called when config.OPENALEX_ENABLED is True.

        Adds fields:
          - cited_by_count: int
          - concepts: list[str] (top 5 concept labels)
          - oa_status: str (open access status)
        """
        if not config.OPENALEX_ENABLED or not papers:
            return papers

        logger.info(f"  OpenAlex: enriching {len(papers)} papers ...")
        enriched = 0
        for paper in papers:
            arxiv_id = paper["arxiv_id"]
            oa_data = self._query_openalex(arxiv_id)
            if oa_data:
                paper["cited_by_count"] = oa_data.get("cited_by_count", 0)
                paper["concepts"] = [
                    c.get("display_name", "")
                    for c in (oa_data.get("concepts") or [])[:5]
                    if c.get("display_name")
                ]
                paper["oa_status"] = oa_data.get("open_access", {}).get("oa_status", "")
                enriched += 1
            else:
                paper["cited_by_count"] = 0
                paper["concepts"] = []
                paper["oa_status"] = ""

        logger.info(f"  OpenAlex: enriched {enriched}/{len(papers)} papers")
        return papers

    def _query_openalex(self, arxiv_id: str) -> Optional[dict]:
        """Query OpenAlex for a single arXiv paper by its ID."""
        doi_url = f"https://arxiv.org/abs/{arxiv_id}"
        url = f"{config.OPENALEX_BASE}/works/{quote(doi_url, safe='')}"
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"  OpenAlex miss for {arxiv_id}: {e}")
            return None

    def fetch_via_openalex(
        self,
        cutoff_date: date,
        is_known_fn: Optional[Callable[[str], bool]] = None,
    ) -> list[dict]:
        """
        Supplementary fetch via OpenAlex API.

        OpenAlex indexes arXiv papers and can serve as a backup when
        arXiv RSS/API are unavailable. Returns papers in the same dict
        format as _fetch_via_rss.
        """
        logger.info("  OpenAlex: fetching recent arXiv papers ...")
        url = f"{config.OPENALEX_BASE}/works"
        params = {
            "filter": (
                f"from_publication_date:{cutoff_date.isoformat()},"
                f"primary_location.source.id:S4306419638"  # arXiv source
            ),
            "per_page": 200,
            "sort": "publication_date:desc",
            "mailto": "polymer-scanner@example.com",
        }

        resp = self._fetch_with_retry(
            f"{url}?{urlencode(params)}",
            timeout=20,
            max_retries=2,
        )
        if resp is None:
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"  OpenAlex JSON parse error: {e}")
            return []

        papers: list[dict] = []
        for work in data.get("results", []):
            # Extract arxiv_id from the work's DOI or title
            doi = work.get("doi", "") or ""
            arxiv_id = ""
            if "arxiv" in doi.lower():
                arxiv_id = doi.split("arxiv.")[-1].split("/")[-1].strip()
            else:
                # Try to find arxiv ID in locations
                for loc in work.get("locations", []):
                    landing_url = loc.get("landing_page_url", "") or ""
                    if "arxiv.org/abs/" in landing_url:
                        arxiv_id = landing_url.split("/abs/")[-1].strip()
                        break

            if not arxiv_id:
                continue
            if is_known_fn and is_known_fn(arxiv_id):
                continue

            title = work.get("title", "") or ""
            abstract_inverted = work.get("abstract_inverted_index", {})
            abstract = self._invert_index_to_text(abstract_inverted)

            authors = [
                a.get("author", {}).get("display_name", "")
                for a in work.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ][:10]

            pub_date_str = work.get("publication_date", "")
            try:
                pub_date = date.fromisoformat(pub_date_str)
            except (ValueError, TypeError):
                pub_date = date.today()

            papers.append({
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": list(self.categories),
                "published": pub_date,
                "pdf_url": f"{config.ARXIV_BASE}/pdf/{arxiv_id}",
                "is_updated": False,
                "cited_by_count": work.get("cited_by_count", 0),
                "concepts": [
                    c.get("display_name", "")
                    for c in (work.get("concepts") or [])[:5]
                    if c.get("display_name")
                ],
            })

        logger.info(f"  OpenAlex: got {len(papers)} papers")
        return papers

    @staticmethod
    def _invert_index_to_text(inverted_index: dict) -> str:
        """Convert OpenAlex inverted index abstract back to plain text."""
        if not inverted_index:
            return ""
        max_pos = 0
        for positions in inverted_index.values():
            for pos in positions:
                max_pos = max(max_pos, pos)
        words = [""] * (max_pos + 1)
        for word, positions in inverted_index.items():
            for pos in positions:
                if pos <= max_pos:
                    words[pos] = word
        return " ".join(words)
