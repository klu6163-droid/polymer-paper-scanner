"""Regression tests for daily_polymer._format_note — single frontmatter, no dup."""
import re

from daily_polymer import _format_note


def _make_result(notes_body="## 一句话总结\n正文。"):
    return {
        "arxiv_id": "2401.12345",
        "title": "Cool Polymer Dynamics",
        "abstract": "We study polymer dynamics.",
        "authors": ["Alice", "Bob"],
        "categories": ["cond-mat.soft"],
        "published": "2026-07-04",
        "notes": notes_body,
    }


def test_single_frontmatter():
    # 3 `---` markers = 1 frontmatter block (open+close) + 1 hr before Reading Notes
    note = _format_note(_make_result())
    markers = re.findall(r"^---$", note, re.M)
    assert len(markers) == 3


def test_no_inner_skill_category():
    # Regression: agent's inner frontmatter used to leak skill_category.
    note = _format_note(_make_result())
    assert "skill_category:" not in note


def test_title_appears_once():
    note = _format_note(_make_result())
    assert note.count("# Cool Polymer Dynamics") == 1


def test_published_in_frontmatter():
    note = _format_note(_make_result())
    assert 'published: "2026-07-04"' in note


def test_notes_body_preserved():
    note = _format_note(_make_result("## 一句话总结\n这是正文。"))
    assert "这是正文。" in note
