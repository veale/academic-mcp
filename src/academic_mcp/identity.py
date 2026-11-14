"""Document identity verification.

Every full-text source other than the requested record itself can return the
wrong work: a web search hit that merely cites the paper, a repository record
with a mislinked file, a CORE title match, a publisher redirect to a related
article.  Text is accepted under an identifier only when it carries that
record's title near the start; mismatches are rejected before they are
returned, cached, or written to Zotero.

The check is deliberately text-only (no network) so it can run on every fetch
and on every cached entry.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from dataclasses import asdict, dataclass

# Statuses
PASS = "pass"
MISMATCH = "mismatch"
UNCERTAIN = "uncertain"
NOT_CHECKABLE = "not_checkable"

# Head of the document that must carry the title.  Cover sheets (JSTOR, SSRN,
# repository front pages) and book front matter put the title well inside
# this window; a paper that merely cites the work rarely does.
_HEAD_CHARS = 6000
# Folded characters within which a verbatim title counts as the title page.
_TITLE_PAGE_CHARS = 2500

_STOPWORDS = frozenset(
    """a an the and or of in on for to with from by at as is are was were be
    its it this that these those into onto over under between about through
    after before during toward towards via vs versus not no than how what why
    when where which who whom whose do does did can could should would may
    might will shall de la le les des du et en el los las del y una un der die
    das und von zu im am""".split()
)

_CV_MARKERS = ("curriculum vitae", "selected publications", "list of publications")


def _fold(text: str) -> str:
    """Lowercase, strip accents, join line-break hyphenation, keep alnum."""
    text = html_lib.unescape(re.sub(r"</?[a-zA-Z][^>]{0,40}>", "", text or ""))
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("ß", "ss")
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(folded: str) -> list[str]:
    return [t for t in folded.split() if len(t) > 2 and t not in _STOPWORDS]


def _main_title(title: str) -> str:
    """Title before a subtitle separator, if that part is distinctive."""
    for sep in (":", " - ", " – ", " — ", "?", "."):
        head = title.split(sep, 1)[0]
        if head != title and len(_tokens(_fold(head))) >= 3:
            return head
    return title


def _surname(author: object) -> str:
    if isinstance(author, dict):
        name = author.get("lastName") or author.get("family") or author.get("name") or ""
    else:
        name = str(author or "")
    name = name.strip()
    if "," in name:
        name = name.split(",", 1)[0]
    elif " " in name:
        name = name.split()[-1]
    return _fold(name)


@dataclass
class IdentityCheck:
    status: str
    reason: str
    expected_title: str = ""
    found_title: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (PASS, NOT_CHECKABLE, UNCERTAIN)

    @property
    def rejected(self) -> bool:
        return self.status == MISMATCH

    def to_dict(self) -> dict:
        return asdict(self)


def guess_found_title(text: str, pdf_title: str = "") -> str:
    """Best-effort title of the document actually retrieved (for messages)."""
    pdf_title = (pdf_title or "").strip()
    if pdf_title and len(pdf_title) > 8 and not re.search(
        r"(microsoft word|untitled|\.docx?$|\.pdf$|^doi|^\d+$)", pdf_title, re.I
    ):
        return pdf_title[:200]
    for line in (text or "")[:3000].splitlines():
        line = re.sub(r"[#*_§]+", " ", line).strip()
        if 15 <= len(line) <= 220 and len(line.split()) >= 3 and not re.match(
            r"^(--- page|page \d|downloaded|this content|copyright|©|http|www\.|doi)", line, re.I
        ):
            return line[:200]
    return ""


def check_identity(
    text: str,
    expected_title: str | None,
    expected_authors: list | None = None,
    *,
    pdf_title: str = "",
    page_title: str = "",
    html: bool = False,
) -> IdentityCheck:
    """Decide whether *text* is the work titled *expected_title*.

    ``pass``           title found near the start (or the PDF's own title matches)
    ``mismatch``       a different work: most of the title is absent from the head
    ``uncertain``      partial evidence; acceptable but surfaced to the caller
    ``not_checkable``  no usable expected title, or no text to check

    *page_title* is a publisher page's ``citation_title``; *html* marks text
    extracted from an article page body, which normally omits the title.
    """
    expected_title = (expected_title or "").strip()
    found = (page_title or "").strip()[:200] or guess_found_title(text, pdf_title)
    title_tokens = _tokens(_fold(expected_title))
    if len(title_tokens) < 2:
        return IdentityCheck(NOT_CHECKABLE, "no distinctive expected title", expected_title, found)
    body = (text or "").strip()
    if len(body) < 40:
        return IdentityCheck(NOT_CHECKABLE, "no extractable text", expected_title, found)

    head = _fold(body[:_HEAD_CHARS])
    folded_title = _fold(expected_title)
    folded_main = _fold(_main_title(expected_title))
    folded_pdf_title = _fold(pdf_title)

    # A CV or publication list mentions many titles and is never the paper.
    head_lower = body[:_HEAD_CHARS].lower()
    if "curriculum vitae" in head_lower or sum(m in head_lower for m in _CV_MARKERS) >= 2:
        return IdentityCheck(MISMATCH, "document is a CV or publication list", expected_title, found)

    # Decisive positive evidence: the PDF's own title metadata naming this
    # work, or the title verbatim at the very start (title page or cover
    # sheet).  Later in the opening pages it may instead be a citation in a
    # footnote of a different work, so that is only partial evidence.
    if folded_pdf_title and (
        folded_title in folded_pdf_title or (folded_main and folded_main in folded_pdf_title)
    ):
        return IdentityCheck(PASS, "PDF title metadata matches", expected_title, found)
    if page_title:
        verdict = _compare_titles(expected_title, page_title)
        if verdict is not None:
            status, reason = verdict
            return IdentityCheck(status, reason, expected_title, found)

    if html:
        return _check_html_body(body, title_tokens, folded_title, folded_main, expected_title, found)

    position = _title_position(head, folded_title, folded_main)
    if position is not None and position <= _TITLE_PAGE_CHARS:
        return IdentityCheck(PASS, "title found at start of document", expected_title, found)
    if position is not None:
        return IdentityCheck(
            UNCERTAIN, "title appears in the opening pages but not on the title page",
            expected_title, found,
        )

    unique = list(dict.fromkeys(title_tokens))
    title_page = head[:_TITLE_PAGE_CHARS]
    tp_tokens = set(title_page.split())
    head_tokens = set(head.split())
    tp_cov = sum(t in tp_tokens for t in unique) / len(unique)
    head_cov = sum(t in head_tokens for t in unique) / len(unique)
    surnames = [s for s in (_surname(a) for a in (expected_authors or [])[:6]) if len(s) > 1]
    author_on_tp = any(re.search(rf"\b{re.escape(s)}\b", title_page) for s in surnames)

    # OCR noise, line-wrapped or reformatted titles defeat verbatim matching;
    # near-total token coverage on the title page plus an author is still
    # strong evidence.  Anything weaker is a different (often related) work.
    if tp_cov >= 0.85 and (author_on_tp or not surnames):
        return IdentityCheck(PASS, "title words and author found on title page", expected_title, found)
    if tp_cov >= 0.85 or (head_cov >= 0.9 and author_on_tp):
        return IdentityCheck(UNCERTAIN, "title words found near the start; author not confirmed",
                             expected_title, found)
    if author_on_tp and tp_cov >= 0.4:
        # Same authors, overlapping title: often another version of the work
        # (retitled preprint), but sometimes a different paper by them.
        return IdentityCheck(
            UNCERTAIN, "an author matches but the title differs; possibly another version or another paper",
            expected_title, found,
        )
    return IdentityCheck(
        MISMATCH,
        f"title not found at start ({head_cov:.0%} of title words present)",
        expected_title,
        found,
    )


def _compact(folded: str) -> str:
    return folded.replace(" ", "")


def _title_position(haystack: str, folded_title: str, folded_main: str) -> int | None:
    """Earliest position of the title (or a distinctive main title) in *haystack*.

    Also matched with spaces removed, so 'micro-foundations' finds
    'microfoundations' and vice versa.  Positions in the compact form are
    scaled back to approximate folded-character offsets.
    """
    candidates = [folded_title]
    if len(_tokens(folded_main)) >= 3:
        candidates.append(folded_main)
    hits = [haystack.find(c) for c in candidates if c]
    hits = [h for h in hits if h >= 0]
    if hits:
        return min(hits)
    compact = _compact(haystack)
    hits = [compact.find(_compact(c)) for c in candidates if len(_compact(c)) >= 12]
    hits = [h for h in hits if h >= 0]
    if hits:
        return int(min(hits) * 1.15)
    return None


def _compare_titles(expected: str, other: str) -> tuple[str, str] | None:
    """Compare the expected title with a page's declared title."""
    a, b = _fold(expected), _fold(other)
    if not b:
        return None
    main = _fold(_main_title(expected))
    if a == b or a in b or (len(_tokens(main)) >= 3 and main in b) or _compact(a) == _compact(b):
        return PASS, "publisher page title matches"
    if len(_tokens(b)) >= 3 and b in a:
        return PASS, "publisher page title matches (subtitle differs)"
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if ta and len(ta & tb) / len(ta) < 0.5:
        return MISMATCH, "publisher page is for a different title"
    return None


def _check_html_body(body, title_tokens, folded_title, folded_main, expected_title, found):
    """Article-page bodies usually omit the title; judge by the opening text."""
    wide = _fold(body[:20000])
    if _title_position(wide, folded_title, folded_main) is not None:
        return IdentityCheck(PASS, "title found in article page text", expected_title, found)
    words = set(wide.split())
    unique = list(dict.fromkeys(title_tokens))
    coverage = sum(t in words for t in unique) / len(unique)
    if coverage >= 0.6:
        return IdentityCheck(UNCERTAIN, "article page text matches the title's subject; title not shown",
                             expected_title, found)
    return IdentityCheck(MISMATCH, f"article page text does not match the title ({coverage:.0%} of title words)",
                         expected_title, found)


_RANK = {PASS: 0, UNCERTAIN: 1, NOT_CHECKABLE: 2, MISMATCH: 3}


def check_identity_any(text: str, titles: list[str], expected_authors: list | None = None,
                       **kwargs) -> IdentityCheck:
    """Best verdict over every known title of the work.

    A record can carry a preprint title in the library and the published
    title at Crossref; text matching either is the same work.
    """
    titles = [t for t in dict.fromkeys(t.strip() for t in titles if t and t.strip())]
    if not titles:
        return check_identity(text, "", expected_authors, **kwargs)
    best = None
    for title in titles:
        check = check_identity(text, title, expected_authors, **kwargs)
        if best is None or _RANK[check.status] < _RANK[best.status]:
            best = check
        if best.status == PASS:
            break
    return best


# ---------------------------------------------------------------------------
# Content-quality assessment
# ---------------------------------------------------------------------------

_COVER_MARKERS = (
    "your use of the jstor archive",
    "jstor is a not-for-profit",
    "this content downloaded from",
    "electronic copy available at",
    "sage social science collections",
    "all rights reserved",
    "terms and conditions of use",
)


def assess_content(text: str, *, pages: int | None = None) -> list[str]:
    """Return warnings about text that is unlikely to be the full work."""
    warnings: list[str] = []
    body = (text or "").strip()
    stripped = re.sub(r"---\s*Page \d+\s*---", " ", body)
    words = len(stripped.split())
    if pages and pages > 0 and words < 20 * pages and words < 200:
        warnings.append(
            f"no_text_layer: {pages} page(s) but only {words} words extracted; "
            "the PDF is probably scanned images without OCR, so absence of a term is not evidence"
        )
    elif words < 400:
        lower = stripped.lower()
        if any(m in lower for m in _COVER_MARKERS):
            warnings.append(
                f"cover_sheet_only: {words} words, mostly a publisher or archive cover page"
            )
        else:
            warnings.append(f"very_short: only {words} words extracted; this is unlikely to be the full text")
    if words >= 400:
        sample = stripped[: min(len(stripped), 40000)]
        lines = [ln for ln in sample.splitlines() if ln.strip()]
        if lines:
            ref_like = sum(
                1 for ln in lines
                if re.search(r"\(\d{4}\)|\b(19|20)\d{2}\b.*\b(pp?\.|vol\.|journal|review)\b", ln, re.I)
            )
            if ref_like / len(lines) > 0.5 and len(lines) > 30:
                warnings.append(
                    "references_only: the text is mostly bibliography entries; the article body was probably not obtained"
                )
    return warnings
