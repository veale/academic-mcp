"""core.export — render search results as BibTeX or CSL-JSON.

Discovery is only half of a workflow; the other half is getting what you found
into a manuscript. Zotero users have ``zotero_save_items``, but a citation
string is often all that's wanted, and non-Zotero consumers (pandoc, LaTeX,
a reference manager that isn't Zotero) need a portable serialisation.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any, Iterable

# BibTeX special characters. Escaped rather than stripped: a title reading
# "Cost & Benefit" must not silently become "Cost Benefit".
_BIBTEX_ESCAPE = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_KEY_STRIP = re.compile(r"[^a-zA-Z0-9]")

# OpenAlex / Crossref work types → BibTeX entry types and CSL types.
_BIBTEX_TYPES = {
    "book": "book",
    "edited-book": "book",
    "monograph": "book",
    "reference-book": "book",
    "book-chapter": "incollection",
    "bookSection": "incollection",
    "proceedings-article": "inproceedings",
    "conferencePaper": "inproceedings",
    "dissertation": "phdthesis",
    "thesis": "phdthesis",
    "report": "techreport",
    "posted-content": "misc",
    "preprint": "misc",
}

_CSL_TYPES = {
    "book": "book",
    "edited-book": "book",
    "monograph": "book",
    "reference-book": "book",
    "book-chapter": "chapter",
    "bookSection": "chapter",
    "proceedings-article": "paper-conference",
    "conferencePaper": "paper-conference",
    "dissertation": "thesis",
    "thesis": "thesis",
    "report": "report",
    "posted-content": "article",
    "preprint": "article",
}


def _escape_bibtex(value: str) -> str:
    return "".join(_BIBTEX_ESCAPE.get(ch, ch) for ch in value)


def _ascii_fold(value: str) -> str:
    """Strip diacritics so cite keys stay ASCII (\\citep{muller2019...})."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _surname(author: str) -> str:
    """Last word of a display name, or the part before a comma."""
    author = author.strip()
    if not author:
        return ""
    if "," in author:
        return author.split(",", 1)[0].strip()
    return author.split()[-1]


def _cite_key(hit: Any, taken: set[str]) -> str:
    """``surnameYEARfirstword``, disambiguated with a/b/c on collision."""
    authors = list(getattr(hit, "authors", None) or [])
    surname = _KEY_STRIP.sub("", _ascii_fold(_surname(authors[0]))).lower() if authors else ""
    year = str(getattr(hit, "year", "") or "")[:4]
    title_words = (getattr(hit, "title", "") or "").split()
    first = _KEY_STRIP.sub("", _ascii_fold(title_words[0])).lower() if title_words else ""

    base = f"{surname or 'anon'}{year}{first}" or "ref"
    key = base
    suffix = ord("a")
    while key in taken:
        key = f"{base}{chr(suffix)}"
        suffix += 1
    taken.add(key)
    return key


def _entry_type(work_type: str | None) -> str:
    return _BIBTEX_TYPES.get((work_type or "").strip(), "article")


def to_bibtex(hits: Iterable[Any]) -> str:
    """Render search hits as a BibTeX bibliography."""
    taken: set[str] = set()
    entries: list[str] = []

    for hit in hits:
        key = _cite_key(hit, taken)
        entry_type = _entry_type(getattr(hit, "work_type", None))
        fields: list[tuple[str, str]] = []

        title = getattr(hit, "title", None)
        if title:
            # An extra brace pair protects the author's capitalisation from
            # style files that would otherwise lowercase "GDPR" to "gdpr".
            fields.append(("title", "{" + _escape_bibtex(title) + "}"))

        authors = list(getattr(hit, "authors", None) or [])
        if authors:
            fields.append(("author", _escape_bibtex(" and ".join(authors))))

        year = getattr(hit, "year", None)
        if year:
            fields.append(("year", str(year)[:4]))

        venue = getattr(hit, "venue", None)
        if venue:
            venue_field = {
                "inproceedings": "booktitle",
                "incollection": "booktitle",
                "book": "publisher",
                "techreport": "institution",
                "phdthesis": "school",
            }.get(entry_type, "journal")
            fields.append((venue_field, _escape_bibtex(venue)))

        doi = getattr(hit, "doi", None)
        if doi:
            fields.append(("doi", _escape_bibtex(doi)))

        url = getattr(hit, "url", None)
        if url and not doi:
            fields.append(("url", _escape_bibtex(url)))

        abstract = getattr(hit, "abstract", None)
        if abstract and not abstract.startswith("[Preview from"):
            fields.append(("abstract", _escape_bibtex(abstract)))

        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
        entries.append(f"@{entry_type}{{{key},\n{body}\n}}")

    return "\n\n".join(entries)


def _csl_author(name: str) -> dict:
    name = name.strip()
    if "," in name:
        family, _, given = name.partition(",")
        return {"family": family.strip(), "given": given.strip()}
    parts = name.split()
    if len(parts) == 1:
        return {"literal": parts[0]}
    return {"family": parts[-1], "given": " ".join(parts[:-1])}


def to_csl_json(hits: Iterable[Any]) -> str:
    """Render search hits as CSL-JSON (pandoc, Zotero import, citeproc)."""
    taken: set[str] = set()
    records: list[dict] = []

    for hit in hits:
        record: dict[str, Any] = {
            "id": _cite_key(hit, taken),
            "type": _CSL_TYPES.get((getattr(hit, "work_type", None) or "").strip(), "article-journal"),
            "title": getattr(hit, "title", "") or "",
        }

        authors = list(getattr(hit, "authors", None) or [])
        if authors:
            record["author"] = [_csl_author(a) for a in authors]

        year = str(getattr(hit, "year", "") or "")[:4]
        if year.isdigit():
            record["issued"] = {"date-parts": [[int(year)]]}

        venue = getattr(hit, "venue", None)
        if venue:
            record["container-title"] = venue

        for attr, field in (("doi", "DOI"), ("url", "URL")):
            value = getattr(hit, attr, None)
            if value:
                record[field] = value

        abstract = getattr(hit, "abstract", None)
        if abstract and not abstract.startswith("[Preview from"):
            record["abstract"] = abstract

        records.append(record)

    return json.dumps(records, indent=2, ensure_ascii=False)


def render(hits: Iterable[Any], fmt: str) -> str:
    """Dispatch on format name. Raises ``ValueError`` on an unknown format."""
    fmt = (fmt or "").strip().lower()
    if fmt in ("bibtex", "bib"):
        return to_bibtex(hits)
    if fmt in ("csl", "csl-json", "csljson", "json"):
        return to_csl_json(hits)
    raise ValueError(f"Unknown export format {fmt!r}. Use 'bibtex' or 'csl-json'.")


# ---------------------------------------------------------------------------
# DOI -> metadata resolution
# ---------------------------------------------------------------------------

async def _hit_for_doi(doi: str) -> Any | None:
    """Resolve a DOI to a SearchHit — Zotero first, then Crossref."""
    from .. import apis, zotero_sqlite
    from .types import SearchHit

    doi = doi.strip()
    if not doi:
        return None

    try:
        item = await zotero_sqlite.search_by_doi(doi)
    except Exception:
        item = None
    if item:
        return SearchHit(
            title=item.title or "Untitled",
            authors=[c.display_name for c in (item.creators or []) if c.display_name],
            year=(item.date or "")[:4] or None,
            doi=item.DOI or doi,
            venue=item.publicationTitle or None,
            abstract=item.abstractNote or None,
            work_type=item.itemType or None,
            in_zotero=True,
            zotero_key=item.key,
        )

    try:
        work = await apis.crossref_work(doi)
    except Exception:
        work = None
    if not work:
        return None

    authors = []
    for a in work.get("author") or []:
        if a.get("family"):
            authors.append(f"{a.get('given', '')} {a['family']}".strip())
        elif a.get("name"):
            authors.append(a["name"])

    date_parts = (work.get("published") or work.get("issued") or {}).get("date-parts") or [[None]]
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    return SearchHit(
        title=(work.get("title") or ["Untitled"])[0],
        authors=authors,
        year=str(year) if year else None,
        doi=doi,
        venue=(work.get("container-title") or [None])[0],
        work_type=work.get("type"),
    )


async def citations_for_dois(dois: list[str], fmt: str) -> tuple[str, list[str]]:
    """Render citations for *dois*. Returns ``(rendered, unresolved_dois)``."""
    wanted = [d.strip() for d in dois if d and d.strip()]
    if not wanted:
        return "", []

    resolved = await asyncio.gather(
        *(_hit_for_doi(d) for d in wanted), return_exceptions=True
    )

    hits, unresolved = [], []
    for doi, hit in zip(wanted, resolved):
        if isinstance(hit, Exception) or hit is None:
            unresolved.append(doi)
        else:
            hits.append(hit)

    return (render(hits, fmt) if hits else ""), unresolved
