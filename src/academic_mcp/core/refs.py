"""Resolve one free-form reference to something the fetch pipeline can open.

``ref`` accepts whatever a user or a search result has to hand: a DOI in any
form, a Zotero item key or ``zotero://`` link, a Better BibTeX citekey, a URL,
an SSRN or arXiv id, or a title (optionally with an author surname and year).
The library is consulted first; the web only when the library has no match.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_ARXIV_RE = re.compile(
    r"^(?:arxiv[:\s]*)?((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[a-z]{2})?/\d{7}))(?:v\d+)?$", re.I
)
_SSRN_RE = re.compile(r"^ssrn[\s:#.\-]*(?:id[\s:#.\-]*)?(\d{4,8})$", re.I)
_CITEKEY_RE = re.compile(r"^[\w:\-.+/]+$")
_YEAR_RE = re.compile(r"^(1[5-9]|20)\d\d[a-z]?$")
_FILLER = frozenset("et al by and eds ed in".split())


@dataclass
class ResolvedRef:
    doi: str | None = None
    zotero_key: str | None = None
    url: str | None = None
    via: str = ""
    candidates: list[dict] = field(default_factory=list)
    error: str = ""


def classify(ref: str) -> tuple[str, str]:
    """``(kind, value)`` where kind is doi, zotero, url or title."""
    text = (ref or "").strip().strip("<>").strip()
    lowered = text.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi.org/", "doi:", "doi "):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            lowered = text.lower()
            break
    if _DOI_RE.match(text):
        return "doi", text
    match = _ARXIV_RE.match(text)
    if match and (lowered.startswith("arxiv") or "." in match.group(1)):
        return "doi", "10.48550/arxiv." + match.group(1).lower()
    match = _SSRN_RE.match(text)
    if match:
        return "doi", "10.2139/ssrn." + match.group(1)
    if lowered.startswith("zotero://"):
        return "zotero", text
    if re.match(r"^https?://", text, re.I) or re.match(r"^www\.", text, re.I):
        return "url", text if text.lower().startswith("http") else "https://" + text
    if " " not in text and _CITEKEY_RE.match(text):
        return "zotero", text
    return "title", text


def main_title(title: str) -> str:
    """Folded title before its first subtitle separator ("" if it has none)."""
    from ..identity import _fold

    for sep in (":", "?", " - ", " – ", " — ", ". "):
        head, found, _ = (title or "").partition(sep)
        if found and head.strip():
            return _fold(head)
    return ""


def match_title(folded_ref: str, folded_title: str, surnames: list[str], year: str = "",
                folded_main: str = "") -> float | None:
    """Score how well a folded *ref* names a work with *folded_title*.

    None unless the match is strong: *ref* contains the whole title, or its
    main title (before a subtitle separator), or the two are near-identical.
    Remaining words in *ref* must include one of the work's author surnames;
    years and filler words ("et al", "by") are allowed alongside. A main
    title of only two content words ("Slave to the Algorithm") also needs a
    surname.
    """
    from ..identity import _tokens

    title_tokens = _tokens(folded_title)
    if not title_tokens:
        return None
    padded = f" {folded_ref} "
    leftover = None
    score = 0.0
    matched_tokens = len(title_tokens)
    if f" {folded_title} " in padded:
        leftover, score = padded.replace(f" {folded_title} ", " ", 1), 1.0
    elif folded_main and folded_main != folded_title and f" {folded_main} " in padded:
        matched_tokens = len(_tokens(folded_main))
        if matched_tokens >= 2:
            leftover, score = padded.replace(f" {folded_main} ", " ", 1), 0.9
    if leftover is None:
        if abs(len(folded_ref) - len(folded_title)) > 0.15 * max(len(folded_title), 1):
            return None
        ratio = difflib.SequenceMatcher(None, folded_ref, folded_title).ratio()
        if ratio < 0.92:
            return None
        leftover, score = "", 0.9 * ratio
    rest = [t for t in leftover.split() if t not in _FILLER and len(t) > 1]
    years = [t for t in rest if _YEAR_RE.match(t)]
    rest = [t for t in rest if not _YEAR_RE.match(t)]
    named = False
    if rest:
        named = any(name in rest or name.replace(" ", "") in rest for name in surnames if name)
        # Surname plus at most a few given names or initials.
        if not named or len(rest) > 6:
            return None
        score += 0.05
    if matched_tokens < 3 and not named:
        # A short bare title ("Introduction") identifies nothing on its own.
        return None
    if years and year:
        score += 0.03 if years[0][:4] == year else -0.1
    return score


def _same_work(a: dict, b: dict) -> bool:
    """Same DOI, or the same title (a preprint and its published version
    usually share one; the published copy is ranked first)."""
    from ..identity import _fold
    if a.get("doi") and b.get("doi") and a["doi"].lower() == b["doi"].lower():
        return True
    return _fold(a.get("title", "")) == _fold(b.get("title", ""))


async def _resolve_title(text: str) -> ResolvedRef:
    from .. import zotero_sqlite
    matches = await zotero_sqlite.find_by_title(text)
    if matches:
        summaries = [zotero_sqlite.candidate_summary(item, atts) for item, atts, _ in matches]
        first = summaries[0]
        if all(_same_work(first, other) for other in summaries[1:]):
            # Duplicates of one work: matches are ranked with the copy that
            # has the user's own PDF first.
            item = matches[0][0]
            return ResolvedRef(
                zotero_key=item.key, doi=item.DOI or None, via="title",
                candidates=summaries if len(summaries) > 1 else [],
            )
        return ResolvedRef(
            via="title", candidates=summaries,
            error=(
                f"ambiguous_reference: '{text}' matches {len(summaries)} different library items. "
                "Call again with ref set to one of their zotero_key values."
            ),
        )
    found = await _resolve_title_web(text)
    if found:
        return ResolvedRef(doi=found, via="title_web")
    return ResolvedRef(
        via="title",
        error=(
            f"unknown_reference: no library item or registered publication has the title '{text}'. "
            "Give the exact title (optionally with the first author's surname and year), a DOI, "
            "or search with search_zotero / search_papers."
        ),
    )


async def _resolve_title_web(text: str) -> str | None:
    """DOI of the single OpenAlex work whose title strongly matches *text*."""
    from .. import apis
    from ..identity import _fold, _surname
    folded = _fold(text)
    try:
        async with apis._client() as client:
            resp = await apis._request_with_retry(
                client, "GET", f"{apis.OA_BASE}/works",
                params={"search": text, "per_page": 8, **apis._openalex_mailto_param()},
                headers=apis._openalex_headers(),
            )
        works = resp.json().get("results") or [] if resp.status_code == 200 else []
    except Exception as exc:
        logger.debug("OpenAlex title lookup failed for %r: %s", text, exc)
        return None
    best: tuple[float, str] | None = None
    for work in works:
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        if not doi:
            continue
        names = [_surname((a.get("author") or {}).get("display_name") or "")
                 for a in work.get("authorships") or []]
        score = match_title(folded, _fold(work.get("title") or ""), names,
                            str(work.get("publication_year") or ""), main_title(work.get("title") or ""))
        if score is not None and (best is None or score > best[0]):
            best = (score, doi)
    return best[1] if best else None


async def resolve(ref: str) -> ResolvedRef:
    kind, value = classify(ref)
    if kind == "doi":
        return ResolvedRef(doi=value, via="doi")
    if kind == "url":
        return ResolvedRef(url=value, via="url")
    if kind == "zotero":
        return ResolvedRef(zotero_key=value, via="zotero")
    return await _resolve_title(value)


def format_candidates(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        who = ", ".join(c.get("authors") or []) or "unknown author"
        pdf = f"PDF: {c['attachment'] or 'yes'}" if c.get("has_pdf") else "no PDF"
        flags = " [auto-imported]" if c.get("auto_imported") else ""
        lib = f" in {c['library']}" if c.get("library") else ""
        lines.append(
            f"  - zotero_key={c['zotero_key']}{lib}: {who} ({c.get('year') or 'n.d.'}) "
            f"'{c.get('title', '')[:120]}'" + (f" doi={c['doi']}" if c.get("doi") else "")
            + f"; {pdf}{flags}"
        )
    return "\n".join(lines)


async def cache_target(entry: str) -> tuple[str, dict, str]:
    """``(cache_key, fetch_fulltext args, error)`` for one batch entry.

    The cache key is what the fetch pipeline stores the text under, so a
    batch can find it again: the DOI when known, else ``zotero:KEY`` or
    ``url:HASH``.
    """
    import hashlib

    from .. import zotero, zotero_sqlite

    if re.match(r"^url:[0-9a-f]{16}$", entry):
        return entry, {}, ""
    if re.match(r"^zotero:[A-Z0-9]{8}$", entry):
        return entry, {"zotero_key": entry.split(":", 1)[1]}, ""
    kind, value = classify(entry)
    if kind == "doi":
        doi = zotero._normalize_doi(value)
        return doi, {"doi": doi}, ""
    item = None
    if kind in ("zotero", "url"):
        try:
            item, _ = await zotero_sqlite.resolve_reference(value)
        except Exception as exc:
            logger.debug("Library lookup failed for %r: %s", value, exc)
        if item is None and kind == "zotero":
            return entry, {}, (
                f"unknown_reference: '{entry}' is not a Zotero item key or citation key "
                "in the synced libraries."
            )
        if item is None:
            return "url:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], {"url": value}, ""
    else:
        resolved = await _resolve_title(value)
        if resolved.error:
            extra = ("\n" + format_candidates(resolved.candidates)) if resolved.candidates else ""
            return entry, {}, resolved.error + extra
        if not resolved.zotero_key:
            doi = zotero._normalize_doi(resolved.doi or "")
            return doi, {"doi": doi}, ""
        found = await zotero_sqlite.search_by_keys([resolved.zotero_key])
        item = found.get(resolved.zotero_key)
        if item is None:
            return entry, {}, f"unknown_reference: library item {resolved.zotero_key} could not be read."
    doi = zotero._normalize_doi(item.DOI) if item.DOI else ""
    return (doi or f"zotero:{item.key}"), {"zotero_key": item.key, "doi": doi or None}, ""
