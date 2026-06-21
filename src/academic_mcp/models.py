"""Data models for the academic MCP server.

Strict data contracts between API fetchers, SQLite backend, and MCP tool
formatters. Using dataclasses instead of raw dicts prevents KeyError bugs
during refactoring and makes the data flow self-documenting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Zotero item types that are legal materials. These often lack a `date` field
# (cases carry `dateDecided`, statutes `dateEnacted`), so they must not be
# excluded by a year filter, and their metadata lives in type-specific fields.
LEGAL_ITEM_TYPES = frozenset({"case", "statute", "bill", "hearing"})


def _clean_zotero_date(value: str) -> str:
    """Zotero stores dates as ``"YYYY-MM-DD <user text>"``; show the user text."""
    value = (value or "").strip()
    if not value:
        return ""
    parts = value.split(" ", 1)
    # Leading token looks like an ISO/SQL date → prefer the human-entered tail.
    if len(parts) == 2 and len(parts[0]) >= 4 and parts[0][:4].isdigit():
        return parts[1].strip() or parts[0]
    return value


def legal_reference_note(item_type: str, fields: dict[str, str]) -> str:
    """Build a free-text citation line for legal materials.

    Metadata for cases/legislation/bills lives in inconsistent, type-specific
    fields, so rather than mapping each to a fixed slot we surface whatever is
    present as a compact ``label: value`` line shown before the abstract.
    """
    def g(*names: str) -> str:
        for n in names:
            v = (fields.get(n) or "").strip()
            if v:
                return v
        return ""

    parts: list[str] = []
    it = (item_type or "").lower()
    if it == "case":
        if (v := _clean_zotero_date(g("dateDecided"))):
            parts.append(f"Decided: {v}")
        if (v := g("docketNumber")):
            parts.append(f"Docket: {v}")
        if (v := g("reporter")):
            parts.append(f"Reporter: {v}")
        if (v := g("court")):
            parts.append(f"Court: {v}")
    elif it == "statute":
        if (v := _clean_zotero_date(g("dateEnacted"))):
            parts.append(f"Enacted: {v}")
        code = " ".join(x for x in (g("code"), g("codeNumber")) if x)
        if code:
            parts.append(f"Code: {code}")
        if (v := g("section")):
            parts.append(f"§ {v}")
        if (v := g("publicLawNumber")):
            parts.append(f"Public Law: {v}")
    elif it == "bill":
        if (v := g("billNumber")):
            parts.append(f"Bill No.: {v}")
        if (v := g("legislativeBody")):
            parts.append(v)
        if (v := g("session")):
            parts.append(f"Session: {v}")
        if (v := _clean_zotero_date(g("date"))):
            parts.append(v)
    elif it == "hearing":
        if (v := g("committee")):
            parts.append(v)
        if (v := g("legislativeBody")):
            parts.append(v)
        if (v := _clean_zotero_date(g("date"))):
            parts.append(v)
    return " · ".join(parts)


@dataclass
class Creator:
    """A paper author or editor."""
    firstName: str = ""
    lastName: str = ""
    creatorType: str = "author"

    @property
    def display_name(self) -> str:
        return f"{self.firstName} {self.lastName}".strip() or "Unknown"


@dataclass
class ZoteroItem:
    """An item from Zotero (SQLite or API)."""
    itemID: int = 0
    key: str = ""
    libraryID: int = 0
    libraryName: str = ""
    libraryType: str = "user"   # "user" or "group"
    groupID: Optional[int] = None  # numeric group ID (group libraries only)
    itemType: str = ""
    title: str = ""
    DOI: str = ""
    url: str = ""
    date: str = ""
    abstractNote: str = ""
    publicationTitle: str = ""
    creators: list[Creator] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    extra: str = ""
    dateAdded: str = ""
    dateModified: str = ""
    fields: dict[str, str] = field(default_factory=dict)  # raw type-specific fields
    _match_type: str = ""       # "metadata" or "fulltext" (for search results)

    @property
    def reference_note(self) -> str:
        """Free-text legal citation line (empty for non-legal items)."""
        return legal_reference_note(self.itemType, self.fields)

    def to_search_result(self) -> dict:
        """Convert to the dict format expected by server.py search handlers."""
        return {
            "key": self.key,
            "title": self.title,
            "creators": [
                {"firstName": c.firstName, "lastName": c.lastName,
                 "creatorType": c.creatorType}
                for c in self.creators
            ],
            "DOI": self.DOI,
            "url": self.url or "",
            "date": self.date,
            "abstractNote": self.abstractNote,
            "publicationTitle": self.publicationTitle,
            "itemType": self.itemType,
            "referenceNote": self.reference_note,
            "libraryName": self.libraryName,
            "libraryType": self.libraryType,
            "groupID": self.groupID,
            "_match_type": self._match_type,
        }


@dataclass
class PaperContent:
    """Full content retrieved for a paper."""
    found: bool = False
    item_key: str = ""
    title: str = ""
    DOI: str = ""
    creators: list[Creator] = field(default_factory=list)
    date: str = ""
    abstractNote: str = ""
    publicationTitle: str = ""
    itemType: str = ""
    url: str = ""
    libraryName: str = ""
    text: Optional[str] = None
    pdf_path: Optional[Path] = None
    source: Optional[str] = None
    truncated: bool = False
    indexed_pages: Optional[int] = None
    total_pages: Optional[int] = None

    @property
    def has_content(self) -> bool:
        return bool(self.text or self.pdf_path)

    def to_zotero_result(self) -> dict:
        """Convert to the dict format expected by get_paper_from_zotero callers."""
        return {
            "found": self.found,
            "item_key": self.item_key,
            "metadata": {
                "title": self.title,
                "creators": [
                    {"firstName": c.firstName, "lastName": c.lastName,
                     "creatorType": c.creatorType}
                    for c in self.creators
                ],
                "DOI": self.DOI,
                "date": self.date,
                "abstractNote": self.abstractNote,
                "publicationTitle": self.publicationTitle,
                "itemType": self.itemType,
                "url": self.url,
            },
            "text": self.text,
            "pdf_path": self.pdf_path,
            "source": self.source,
            "truncated": self.truncated,
            "indexed_pages": self.indexed_pages,
            "total_pages": self.total_pages,
            "libraryName": self.libraryName,
        }


@dataclass
class PDFCandidate:
    """A candidate URL for fetching a PDF."""
    url: str
    source: str  # e.g. "unpaywall", "semantic_scholar", "openalex"

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "source": self.source}


@dataclass
class LibraryInfo:
    """Summary info about a Zotero library."""
    libraryID: int
    type: str           # "user" or "group"
    name: str
    groupID: Optional[int] = None
    itemCount: int = 0

    def to_dict(self) -> dict:
        d = {
            "libraryID": self.libraryID,
            "type": self.type,
            "name": self.name,
            "itemCount": self.itemCount,
        }
        if self.groupID is not None:
            d["groupID"] = self.groupID
        return d


@dataclass
class AttachmentInfo:
    """Info about a PDF attachment in Zotero."""
    itemID: int
    key: str
    path: str = ""
    linkMode: int = 0

    @property
    def storage_dir_name(self) -> str:
        """The 8-char folder name under storage/."""
        return self.key


@dataclass
class FulltextInfo:
    """Fulltext indexing stats from the fulltextItems table."""
    indexedPages: Optional[int] = None
    totalPages: Optional[int] = None
    indexedChars: Optional[int] = None
    totalChars: Optional[int] = None
    version: int = 0

    @property
    def is_truncated(self) -> bool:
        if self.indexedPages and self.totalPages:
            if self.indexedPages < self.totalPages:
                return True
        if self.indexedChars and self.totalChars:
            if self.indexedChars < self.totalChars:
                return True
        return False
