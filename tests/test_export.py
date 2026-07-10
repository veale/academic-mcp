"""Tests for BibTeX / CSL-JSON rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.core import export
from academic_mcp.core.types import SearchHit


def _hit(**kw) -> SearchHit:
    kw.setdefault("title", "A Paper")
    return SearchHit(**kw)


def test_bibtex_basic_entry():
    out = export.to_bibtex([
        _hit(title="Slave to the Algorithm", authors=["Lilian Edwards", "Michael Veale"],
             year="2017", venue="Duke Law Review", doi="10.2139/ssrn.2972855"),
    ])
    assert out.startswith("@article{edwards2017slave,")
    assert "author = {Lilian Edwards and Michael Veale}" in out
    assert "year = {2017}" in out
    assert "journal = {Duke Law Review}" in out
    assert "doi = {10.2139/ssrn.2972855}" in out


def test_bibtex_escapes_special_characters():
    import re

    out = export.to_bibtex([_hit(title="Cost & Benefit: 100% of the $ and #1", authors=["A B"])])
    assert r"\&" in out
    assert r"\%" in out
    assert r"\$" in out
    assert r"\#" in out
    # No occurrence of a special character survives without its backslash.
    assert not re.search(r"(?<!\\)[&%$#]", out)


def test_bibtex_protects_title_capitalisation():
    out = export.to_bibtex([_hit(title="The GDPR and NIST", authors=["A B"])])
    assert "title = {{The GDPR and NIST}}" in out


def test_bibtex_entry_type_from_work_type():
    cases = {
        "book-chapter": "@incollection",
        "proceedings-article": "@inproceedings",
        "book": "@book",
        "thesis": "@phdthesis",
        "report": "@techreport",
        None: "@article",
    }
    for work_type, expected in cases.items():
        out = export.to_bibtex([_hit(work_type=work_type, authors=["A B"], year="2020")])
        assert out.startswith(expected), f"{work_type} -> {out[:20]}"


def test_bibtex_venue_field_follows_entry_type():
    chapter = export.to_bibtex([_hit(work_type="book-chapter", venue="Big Book", authors=["A B"])])
    assert "booktitle = {Big Book}" in chapter

    thesis = export.to_bibtex([_hit(work_type="thesis", venue="UCL", authors=["A B"])])
    assert "school = {UCL}" in thesis


def test_cite_keys_disambiguate_on_collision():
    hits = [
        _hit(title="Fairness Metrics", authors=["Michael Veale"], year="2018"),
        _hit(title="Fairness Metrics", authors=["Michael Veale"], year="2018"),
        _hit(title="Fairness Metrics", authors=["Michael Veale"], year="2018"),
    ]
    out = export.to_bibtex(hits)
    assert "@article{veale2018fairness," in out
    assert "@article{veale2018fairnessa," in out
    assert "@article{veale2018fairnessb," in out


def test_cite_key_folds_diacritics():
    out = export.to_bibtex([_hit(title="Über Alles", authors=["Jürgen Müller"], year="2019")])
    assert "@article{muller2019uber," in out


def test_cite_key_handles_missing_author():
    out = export.to_bibtex([_hit(title="Anonymous Report", year="2019")])
    assert "@article{anon2019anonymous," in out


def test_bibtex_omits_preview_abstracts():
    out = export.to_bibtex([
        _hit(authors=["A B"], abstract="[Preview from PDF page 1]: junk text"),
    ])
    assert "abstract" not in out


def test_bibtex_url_only_when_no_doi():
    with_doi = export.to_bibtex([_hit(authors=["A B"], doi="10.1/x", url="https://e.com")])
    assert "url =" not in with_doi

    without_doi = export.to_bibtex([_hit(authors=["A B"], url="https://e.com")])
    assert "url = {https://e.com}" in without_doi


def test_csl_json_structure():
    out = json.loads(export.to_csl_json([
        _hit(title="A Paper", authors=["Lilian Edwards", "Veale, Michael"],
             year="2017", venue="MLR", doi="10.1/x"),
    ]))
    assert len(out) == 1
    rec = out[0]
    assert rec["type"] == "article-journal"
    assert rec["author"][0] == {"family": "Edwards", "given": "Lilian"}
    assert rec["author"][1] == {"family": "Veale", "given": "Michael"}
    assert rec["issued"] == {"date-parts": [[2017]]}
    assert rec["container-title"] == "MLR"
    assert rec["DOI"] == "10.1/x"


def test_csl_json_single_name_author_uses_literal():
    out = json.loads(export.to_csl_json([_hit(authors=["UNESCO"])]))
    assert out[0]["author"][0] == {"literal": "UNESCO"}


def test_csl_json_type_mapping():
    out = json.loads(export.to_csl_json([_hit(work_type="book-chapter", authors=["A B"])]))
    assert out[0]["type"] == "chapter"


def test_csl_json_omits_non_numeric_year():
    out = json.loads(export.to_csl_json([_hit(authors=["A B"], year="n.d.")]))
    assert "issued" not in out[0]


def test_render_dispatch():
    hits = [_hit(authors=["A B"])]
    assert export.render(hits, "bibtex").startswith("@article")
    assert export.render(hits, "bib").startswith("@article")
    json.loads(export.render(hits, "csl-json"))
    json.loads(export.render(hits, "csljson"))

    with pytest.raises(ValueError, match="Unknown export format"):
        export.render(hits, "endnote")


def test_empty_input_renders_empty():
    assert export.to_bibtex([]) == ""
    assert json.loads(export.to_csl_json([])) == []
