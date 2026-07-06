"""Tests for utils.helpers.extract_arxiv_id — new/old/pdf/bare forms."""
from utils.helpers import extract_arxiv_id


def test_new_style_url_with_version():
    assert extract_arxiv_id("https://arxiv.org/abs/2401.12345v1") == "2401.12345"


def test_new_style_url_no_version():
    assert extract_arxiv_id("https://arxiv.org/abs/2401.12345") == "2401.12345"


def test_old_style_url():
    # Regression: old archive/YYMMNNN ids used to lose the archive prefix.
    assert extract_arxiv_id("https://arxiv.org/abs/cond-mat/0501001v2") == "cond-mat/0501001"


def test_pdf_url():
    assert extract_arxiv_id("https://arxiv.org/pdf/2401.12345.pdf") == "2401.12345"


def test_bare_id():
    assert extract_arxiv_id("2401.12345v1") == "2401.12345"


def test_empty_string():
    assert extract_arxiv_id("") == ""


def test_sanitize_filename_basic():
    from utils.helpers import sanitize_filename
    assert sanitize_filename("Hello, World!") == "Hello-World"
