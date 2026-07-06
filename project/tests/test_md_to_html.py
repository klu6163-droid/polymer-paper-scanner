"""Tests for translate_polymer._md_to_html — escaping, code/blockquote/table, URL sanitize."""
import translate_polymer as tp


def test_html_escaping():
    html = tp._md_to_html("raw <b>tag</b> and <script>x</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html


def test_code_block():
    html = tp._md_to_html("```python\nx = 1\n```")
    assert "<pre><code>" in html
    assert "x = 1" in html


def test_code_block_html_escaped():
    html = tp._md_to_html("```\n<b>not a tag</b>\n```")
    assert "<b>not a tag</b>" not in html
    assert "&lt;b&gt;" in html


def test_blockquote():
    html = tp._md_to_html("> a quote")
    assert "<blockquote>" in html


def test_table():
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    html = tp._md_to_html(md)
    assert '<table class="md-table">' in html
    assert "<th>" in html
    assert "<td>" in html


def test_javascript_link_neutralized():
    html = tp._md_to_html("[x](javascript:alert(1))")
    assert 'href="javascript:alert(1)"' not in html


def test_safe_link_preserved():
    html = tp._md_to_html("[x](https://arxiv.org/abs/2401.12345)")
    assert 'href="https://arxiv.org/abs/2401.12345"' in html


def test_inline_bold_italic_code():
    html = tp._md_to_html("**b** and *i* and `c`")
    assert "<b>b</b>" in html
    assert "<i>i</i>" in html
    assert "<code>c</code>" in html


def test_headings():
    html = tp._md_to_html("## Section\n### Sub")
    assert "<h2>Section</h2>" in html
    assert "<h3>Sub</h3>" in html
