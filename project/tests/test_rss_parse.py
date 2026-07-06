"""Regression tests for arXiv RSS parsing.

Covers three fixes:
  - the ElementTree leaf-element bool bug that made every item parse as None
  - dc:date parsing for the `published` field (was date.today())
  - stripping the "arXiv:ID Announce Type: ... Abstract:" prefix from abstracts
"""
from datetime import date

from arxiv_fetcher.fetcher import ArxivFetcher

RSS_XML = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rss="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <rss:item>
    <rss:title>Cool Polymer Dynamics (arXiv:2401.12345v1 [cond-mat.soft])</rss:title>
    <rss:link>https://arxiv.org/abs/2401.12345</rss:link>
    <rss:description>&lt;p&gt;arXiv:2401.12345v1 Announce Type: cross&lt;/p&gt;&lt;p&gt;Abstract: We study polymer dynamics.&lt;/p&gt;</rss:description>
    <dc:creator>&lt;a href=x&gt;Alice&lt;/a&gt;, &lt;a href=y&gt;Bob&lt;/a&gt;</dc:creator>
    <dc:date>2026-07-04T00:00:00Z</dc:date>
  </rss:item>
</rdf:RDF>"""


def _parse():
    return ArxivFetcher()._parse_rss(RSS_XML, "cond-mat.soft")[0]


def test_rss_returns_items():
    # Regression: leaf Element `find(...) or find(...)` bug returned 0 items.
    papers = ArxivFetcher()._parse_rss(RSS_XML, "cond-mat.soft")
    assert len(papers) == 1


def test_published_from_dc_date():
    p = _parse()
    assert p["published"] == date(2026, 7, 4)


def test_abstract_prefix_stripped():
    p = _parse()
    assert p["abstract"] == "We study polymer dynamics."
    assert "Announce Type" not in p["abstract"]


def test_authors_parsed():
    p = _parse()
    assert p["authors"] == ["Alice", "Bob"]


def test_categories_from_title_bracket():
    p = _parse()
    assert p["categories"] == ["cond-mat.soft"]


def test_arxiv_id_and_pdf_url():
    p = _parse()
    assert p["arxiv_id"] == "2401.12345"
    assert p["pdf_url"].endswith("/pdf/2401.12345")
