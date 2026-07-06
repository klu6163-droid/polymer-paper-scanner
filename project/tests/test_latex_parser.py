"""Tests for paper_reader.latex_parser.parse_latex."""
from paper_reader.latex_parser import parse_latex

LATEX = r"""
\begin{abstract}
This is the abstract.
\end{abstract}

\section{Introduction}
Intro text.

\section{Methods}
Method text.

\section{References}
Refs.

\appendix

\section{Appendix Details}
Appendix text.
"""


def test_abstract_extracted():
    p = parse_latex(LATEX)
    assert "abstract" in p.abstract.lower()


def test_sections_parsed():
    p = parse_latex(LATEX)
    titles = [s.title for s in p.sections]
    assert "Introduction" in titles
    assert "Methods" in titles


def test_appendix_detected():
    p = parse_latex(LATEX)
    assert p.has_appendix
    assert any(s.title == "Appendix Details" for s in p.appendix_sections)


def test_first_pass_text_includes_intro():
    p = parse_latex(LATEX)
    assert "Introduction" in p.first_pass_text


def test_main_body_excludes_intro_and_refs():
    p = parse_latex(LATEX)
    assert "Methods" in p.main_body_text
    assert "Introduction" not in p.main_body_text
