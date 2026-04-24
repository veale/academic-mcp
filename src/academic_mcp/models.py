"""Data models for the academic MCP server.

Strict data contracts between API fetchers, SQLite backend, and MCP tool
formatters. Using dataclasses instead of raw dicts prevents KeyError bugs
during refactoring and makes the data flow self-documenting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    _match_type: str = ""       # "metadata" or "fulltext" (for search results)

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
            "libraryName": self.libraryName,
            "libraryType": self.libraryType,
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
