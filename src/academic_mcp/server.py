"""MCP Server — tool definitions and request handlers."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from mcp.server import Server
from mcp.types import Tool, TextContent

try:
    from . import apis, content_extractor, core_api, pdf_fetcher, pdf_extractor, scite, text_cache, web_search, zotero, zotero_import, zotero_sqlite
    from .config import config
    from .reranker import rerank_results
except ImportError:
    from academic_mcp import apis, content_extractor, core_api, pdf_fetcher, pdf_extractor, scite, text_cache, web_search, zotero, zotero_import, zotero_sqlite
    from academic_mcp.config import config
    from academic_mcp.reranker import rerank_results

logger = logging.getLogger(__name__)
server = Server("academic-research")

# ---------------------------------------------------------------------------
# Per-DOI async lock for concurrent fetch protection
# ---------------------------------------------------------------------------

# Per-DOI locks to prevent concurrent fetches for the same paper.
# The lock ensures that if two calls request the same uncached DOI
# simultaneously, the second waits for the first to finish fetching
# and then reads from cache.
_doi_locks: dict[str, asyncio.Lock] = {}
_doi_locks_lock = asyncio.Lock()  # protects the dict itself
_semantic_sync_task: asyncio.Task | None = None
_semantic_empty_hint_shown: bool = False


async def _get_doi_lock(doi: str) -> asyncio.Lock:
    """Get or create an async lock for a specific DOI."""
    async with _doi_locks_lock:
        if doi not in _doi_locks:
            _doi_locks[doi] = asyncio.Lock()
        return _doi_locks[doi]


def _ensure_semantic_background_sync(max_age_hours: int = 24) -> None:
    """Kick off a background semantic sync when stale; never blocks request paths."""
    global _semantic_sync_task

    if _semantic_sync_task and not _semantic_sync_task.done():
        return

    async def _runner() -> None:
        try:
            try:
                from .semantic_index import SemanticIndexUnavailable, get_semantic_index
            except ImportError:
                from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index

            idx = get_semantic_index()
            status = await idx.status()
            last_sync = status.get("last_sync")
            stale = True
            if isinstance(last_sync, str) and last_sync:
                try:
                    from datetime import datetime, timezone

                    ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
                    stale = age_hours > max_age_hours
                except Exception:
                    stale = True
            if stale:
                await idx.sync(force_rebuild=False, include_fulltext=False)
        except SemanticIndexUnavailable:
            return
        except Exception as e:
            logger.debug("Background semantic sync skipped: %s", e)

    try:
        _semantic_sync_task = asyncio.create_task(_runner())
    except RuntimeError:
        pass


def _format_citation_header(doi: str, metadata: dict | None = None) -> str:
    """Format a short citation block for prepending to article text.

    Returns an empty string if no metadata is available.
    """
    if not metadata:
        return ""

    parts = []
    if metadata.get("title"):
        parts.append(f"Title: {metadata['title']}")
    if metadata.get("authors"):
        authors = metadata["authors"]
        if isinstance(authors, list):
            if len(authors) <= 3:
                parts.append(f"Authors: {', '.join(authors)}")
            else:
                parts.append(f"Authors: {', '.join(authors[:3])} et al.")
        elif isinstance(authors, str):
            parts.append(f"Authors: {authors}")
    if metadata.get("year"):
        parts.append(f"Year: {metadata['year']}")
    if metadata.get("venue"):
        parts.append(f"Venue: {metadata['venue']}")
    parts.append(f"DOI: {doi}")

    if not parts:
        return ""

    return "── Citation ──\n" + "\n".join(parts) + "\n──────────────\n\n"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="search_papers",
        description=(
            "Search for academic papers across Zotero, Semantic Scholar, and OpenAlex. "
            "Returns a ranked list with metadata, abstracts, and retrieval options.\n\n"
            "QUERY TIPS: Decompose natural language questions into 2-6 technical keywords. "
            "Example: 'How do bees navigate using magnetic fields?' → "
            "'magnetoreception honeybee navigation geomagnetic'. "
            "Use author:LastName to search by author.\n\n"
            "NEXT STEP: Pass the DOIs from results to batch_sections to survey the "
            "structure of multiple papers at once, or fetch_fulltext(mode='sections') "
            "for a single paper. Results without a DOI show a url field — pass that "
            "to fetch_fulltext(url=...) to retrieve theses, reports, and working papers.\n\n"
            "FOUND A KEY PAPER? Use get_citations(doi) to find papers that build on it, "
            "or get_references(doi) to find its foundations. This is often more productive "
            "than running more keyword searches — ESPECIALLY when the paper is more than a "
            "year old, since any follow-on work will already cite it. Two strong patterns: "
            "(1) call get_citations(doi) with no keywords to see everything that built on it, "
            "or (2) call get_citations(doi, keywords='...') with BROADER/less-specific terms "
            "than your original query — this uses the seed paper as a topical scope and often "
            "surfaces adjacent work that keyword search missed.\n\n"
            "WIDENING A SEARCH: When the keyword results feel saturated, pick the most "
            "relevant seed(s) and call get_citations with exclude_dois=[all DOIs from this "
            "result set, plus the seed itself] so every returned paper is FRESH.\n\n"
            "BOOKS & CHAPTERS: Results tagged [BOOK] or [CHAPTER] support an extra "
            "navigation step — get_book_chapters(doi=...) drills down from a book to its "
            "chapters, or up from a chapter to its siblings. For book chapters, prefer this "
            "over fetching the book as a single PDF.\n\n"
            "Use domain_hint='law' when the query involves legal scholarship, law "
            "review articles, or legal academic research. This triggers a specialised "
            "Primo search constrained to law journals, which covers HeinOnline and "
            "other legal databases not indexed by Semantic Scholar or OpenAlex."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search keywords (e.g. 'attention mechanism vision transformer'). "
                        "Decompose natural language questions into 2-6 technical keywords "
                        "before calling — VERBATIM queries are discouraged. "
                        "Use author:LastName to search by author."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per source (default 5, max 20)",
                    "default": 5,
                },
                "source": {
                    "type": "string",
                    "enum": ["all", "semantic_scholar", "openalex", "zotero", "semantic_zotero", "primo"],
                    "description": (
                        "Which sources to search. 'all' (default) searches Zotero "
                        "(lexical + semantic) + Semantic Scholar + OpenAlex + Primo "
                        "(if configured) and deduplicates. 'zotero' searches your "
                        "library lexically; 'semantic_zotero' searches it via "
                        "embeddings + cross-encoder rerank. 'primo' searches your "
                        "institution's Ex Libris catalogue."
                    ),
                    "default": "all",
                },
                "start_year": {
                    "type": "integer",
                    "description": (
                        "Filter results to papers published on or after this year "
                        "(e.g. 2020). Optional."
                    ),
                },
                "end_year": {
                    "type": "integer",
                    "description": (
                        "Filter results to papers published on or before this year "
                        "(e.g. 2024). Optional."
                    ),
                },
                "venue": {
                    "type": "string",
                    "description": (
                        "Filter results by publication venue name "
                        "(e.g. 'Nature', 'NeurIPS'). Applies to OpenAlex; "
                        "Semantic Scholar results are post-filtered. Optional."
                    ),
                },
                "domain_hint": {
                    "type": "string",
                    "enum": ["general", "law"],
                    "description": (
                        "Domain hint for specialised search strategies. "
                        "'law' triggers an additional Primo search constrained to law "
                        "reviews and legal journals (HeinOnline, Lexis, Gale) — use "
                        "this when searching for law review articles, legal scholarship, "
                        "or any academic legal research. "
                        "'general' (default) uses standard sources."
                    ),
                    "default": "general",
                },
                "include_scite": {
                    "type": "boolean",
                    "description": (
                        "When true, enrich results with Scite tallies and apply a small "
                        "ranking penalty for retraction/correction notices."
                    ),
                    "default": False,
                },
                "semantic": {
                    "type": "boolean",
                    "description": (
                        "Blend semantic search hits from your Zotero index "
                        "(ChromaDB + cross-encoder rerank) with the lexical sources. "
                        "Runs in parallel with the API calls. Default true; set "
                        "false to skip. Disable globally with SEMANTIC_DEFAULT_ON=false."
                    ),
                    "default": True,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_paper",
        description=(
            "Get detailed metadata for a single paper by DOI. Returns title, "
            "abstract, authors, citations, venue, Zotero status, and a preview "
            "snippet. Also tells you what retrieval options are available for "
            "getting the full text.\n\n"
            "To explore the citation network: get_citations(doi) for papers that cite "
            "this one, get_references(doi) for papers it cites."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": (
                        "DOI (e.g. '10.1234/example') or Semantic Scholar paper ID."
                    ),
                },
            },
            "required": ["identifier"],
        },
    ),
    Tool(
        name="get_citations",
        description=(
            "Find papers that CITE a given work (forward citations — its academic "
            "'children'). Use this to trace how a paper's ideas have been developed, "
            "applied, critiqued, or extended by later research.\n\n"
            "⭐ EXPANSION TOOL: When you find a highly relevant paper via search_papers "
            "or get_paper — especially one more than a year old — call this to discover "
            "the research that built on it. This is usually MORE productive than re-running "
            "keyword search, because citing papers are topically anchored to the seed.\n\n"
            "TIP: When filtering with keywords here, use BROADER/less-specific terms than "
            "your original search query. The seed paper already scopes the topic, so you "
            "want the filter to widen — not narrow — what you find (e.g. if the original "
            "search was 'transformer attention long-context retrieval', filter citations "
            "with just 'retrieval' or 'long context' to surface adjacent work).\n\n"
            "Combined with get_references, this lets you map the full citation "
            "neighbourhood of a key paper.\n\n"
            "Optional keyword filtering narrows large citation lists to a subtopic "
            "(e.g. only citations that discuss 'neural networks').\n\n"
            "Results include DOIs — pass them to batch_sections to survey structure, "
            "or fetch_fulltext to read specific papers.\n\n"
            "Sorted by citation count (most-cited first) to surface influential work."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the paper whose citations you want to find.",
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "Optional keyword filter to narrow citations to a subtopic. "
                        "Example: 'machine learning' to find only ML-related citing papers. "
                        "Omit to get all citations."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 25, max 50). High-citation papers may have thousands of citers — use keywords to focus.",
                    "default": 25,
                },
                "start_year": {
                    "type": "integer",
                    "description": "Only include citations from this year onward (e.g. 2020).",
                },
                "end_year": {
                    "type": "integer",
                    "description": "Only include citations up to this year (e.g. 2024).",
                },
                "openalex_id": {
                    "type": "string",
                    "description": (
                        "Optional OpenAlex Work ID (e.g. 'W2741809807'). If provided, "
                        "skips the DOI-to-ID resolution step (faster). You can find this "
                        "in OpenAlex search results or prior citation tool output. "
                        "If omitted, the DOI is resolved automatically."
                    ),
                },
                "exclude_dois": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of DOIs to filter out of the results. Use this when "
                        "widening a search via citations — pass the DOIs you've already seen "
                        "(from your earlier search_papers results, and ideally the seed DOI "
                        "itself) so every result here is FRESH. Matching is case-insensitive "
                        "and tolerates 'https://doi.org/' prefixes."
                    ),
                },
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="get_references",
        description=(
            "Find papers CITED BY a given work (backward references — its academic "
            "'parents'). Use this to understand the intellectual foundations and prior "
            "work that a paper builds upon.\n\n"
            "⭐ EXPANSION TOOL: When you find a highly relevant paper, call this to "
            "discover the foundational works in its reference list. This is especially "
            "useful for literature reviews, understanding theoretical lineage, and "
            "finding seminal papers.\n\n"
            "Optional keyword filtering narrows the reference list to a subtopic.\n\n"
            "Results include DOIs — pass them to batch_sections to survey structure, "
            "or fetch_fulltext to read specific papers.\n\n"
            "Sorted by citation count (most-cited first) to surface foundational work."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the paper whose references you want to find.",
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "Optional keyword filter to narrow references to a subtopic. "
                        "Example: 'randomised controlled trial' to find only RCT references. "
                        "Omit to get all references."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 25, max 50).",
                    "default": 25,
                },
                "start_year": {
                    "type": "integer",
                    "description": "Only include references from this year onward.",
                },
                "end_year": {
                    "type": "integer",
                    "description": "Only include references up to this year.",
                },
                "openalex_id": {
                    "type": "string",
                    "description": (
                        "Optional OpenAlex Work ID (e.g. 'W2741809807'). If provided, "
                        "skips the DOI-to-ID resolution step (faster). If omitted, the "
                        "DOI is resolved automatically."
                    ),
                },
                "exclude_dois": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of DOIs to filter out of the results — useful for "
                        "skipping papers you've already seen when widening a search."
                    ),
                },
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="get_citation_tree",
        description=(
            "Get BOTH forward citations AND backward references for a paper in one call. "
            "Fires both requests concurrently for speed. Use this when you want the full "
            "citation neighbourhood of a key paper.\n\n"
            "Supports keyword filtering (applied to both directions) and year ranges "
            "to keep results focused. Without keywords, highly-cited papers may return "
            "very broad results — use keywords to narrow to your subtopic.\n\n"
            "Returns two sections: papers that cite this work (children) and papers this "
            "work cites (parents). All results include DOIs for further exploration."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the paper to explore.",
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "Optional keyword filter applied to BOTH directions. "
                        "Narrows citations and references to a subtopic. "
                        "Example: 'regulatory compliance' to find only compliance-related neighbours."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per direction (default 10, max 25).",
                    "default": 10,
                },
                "start_year": {
                    "type": "integer",
                    "description": "Only include papers from this year onward.",
                },
                "end_year": {
                    "type": "integer",
                    "description": "Only include papers up to this year.",
                },
                "openalex_id": {
                    "type": "string",
                    "description": (
                        "Optional OpenAlex Work ID. If provided, skips the DOI-to-ID "
                        "resolution — and since this tool makes two concurrent requests, "
                        "providing it avoids the resolution being done twice."
                    ),
                },
                "exclude_dois": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of DOIs to filter out of BOTH directions — useful for "
                        "skipping papers you've already seen when widening a search."
                    ),
                },
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="get_book_chapters",
        description=(
            "List the chapters of an edited volume or monograph. Use this to DRILL DOWN "
            "from a book DOI into its individual chapters (each chapter usually has its "
            "own DOI and can be fetched independently — far more useful than trying to "
            "fetch the whole book PDF, which publishers often don't host). Equally, use "
            "it to DRILL UP from a chapter DOI to see its sibling chapters — a strong "
            "way to discover topically-related work that keyword search tends to miss.\n\n"
            "INPUT: pass either a `doi` (of the book OR any of its chapters) or an `isbn`. "
            "The tool resolves the DOI via Crossref, extracts the ISBN/container-title, "
            "and lists all sibling chapters.\n\n"
            "Optional `keywords` narrows to chapters matching those terms — useful for "
            "edited handbooks where only a subset of chapters is relevant.\n\n"
            "Results include chapter DOIs, authors, page ranges, and Zotero status. "
            "Pass DOIs to fetch_fulltext or batch_sections to read specific chapters.\n\n"
            "LIMITATION: Crossref chapter-of-book coverage is patchy — recent academic "
            "volumes (Springer, Routledge, OUP, CUP) usually have per-chapter DOIs; older "
            "monographs and trade books often don't. If no chapters come back, the book "
            "likely isn't registered at the chapter level."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": (
                        "DOI of either the book itself OR one of its chapters. "
                        "Either doi or isbn must be provided."
                    ),
                },
                "isbn": {
                    "type": "string",
                    "description": (
                        "ISBN of the book (dashes tolerated). Use when you have the ISBN "
                        "but no DOI, or to bypass DOI resolution."
                    ),
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "Optional keyword filter — narrows to chapters whose "
                        "bibliographic metadata matches these terms."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max chapters to return (default 50, max 100).",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="fetch_fulltext",
        description=(
            "Get the full text of a paper for analysis. Checks Zotero first, "
            "then tries open-access sources, stealth browser, and institutional proxy. "
            "After the first fetch, text is cached locally — subsequent calls are instant.\n\n"
            "Accepts any of: doi (preferred when available), zotero_key (for Zotero-only "
            "items without a DOI), or url (for items visible on the web but without a DOI — "
            "typical for theses, reports, working papers, institutional documents). "
            "When multiple are provided, DOI wins. search_papers results now include a url "
            "field when no DOI is available.\n\n"
            "DEFAULT WORKFLOW (mode='sections' — the default):\n"
            "1. Call with no mode argument — returns headings with TF-IDF keywords "
            "revealing what each section discusses.\n"
            "2. Call with mode='section' and the heading you need, OR use "
            "search_in_article to find specific terms.\n"
            "3. Use mode='range' with character offsets from sections or search results "
            "to read specific passages.\n\n"
            "Use mode='full' ONLY when you genuinely need the entire text (e.g. "
            "summarising a short paper end-to-end). For specific questions, "
            "sections → search → range is far more efficient.\n\n"
            "For multiple papers, use batch_sections instead — it fetches and surveys "
            "them all in parallel.\n\n"
            "WHEN SECTIONS ARE POOR: If mode='sections' returns very few sections "
            "(≤2) or the section keywords look uninformative, the cached source may be "
            "a poorly-structured PDF. Try source='html' to fetch a fresh copy of the "
            "publisher's article page via the stealth browser — HTML articles often have "
            "better section structure. The HTML result replaces the cache only if it has "
            ">1500 words and at least 3 parsed sections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the paper (from search_papers results)",
                },
                "zotero_key": {
                    "type": "string",
                    "description": (
                        "Zotero item key — fallback for Zotero-only items that have no DOI. "
                        "Use this when search_papers returns a result with '★ IN ZOTERO' but no DOI."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "Direct URL to the article or its landing page. Use this when "
                        "the search result has no DOI but shows a URL field (common for "
                        "theses, institutional reports, working papers, OECD/ICO guidance). "
                        "The server will attempt a direct PDF download if the URL ends in "
                        ".pdf, then fall back to stealth-browser landing-page extraction. "
                        "Reuses the same HTML extraction pipeline as DOI landing pages."
                    ),
                },
                "use_proxy": {
                    "type": "boolean",
                    "description": "Route uncached fetches through institutional proxy",
                    "default": False,
                },
                "pages": {
                    "type": "string",
                    "description": "Optional page range for PDF sources, e.g. '1-5'. Default: all.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["full", "sections", "preview", "section", "range"],
                    "description": (
                        "What to return. "
                        "'sections' — CALL THIS FIRST (the default) — lists headings with TF-IDF keywords "
                        "showing what each section discusses; large gaps are automatically "
                        "filled with keyword-labelled navigation chunks. "
                        "'section' — returns a specific section by name (fuzzy-matched). "
                        "'preview' — abstract + first paragraph of each section. "
                        "'range' — character slice using range_start/range_end from the "
                        "offsets in sections output or search_in_article results. "
                        "'full' — returns everything (often 50,000+ chars for journal articles)."
                    ),
                    "default": "sections",
                },
                "section": {
                    "type": "string",
                    "description": (
                        "Section name to retrieve (used with mode='section'). "
                        "Fuzzy-matched against headings, e.g. 'introduction' or 'methods'."
                    ),
                },
                "range_start": {
                    "type": "integer",
                    "description": "Start character offset (used with mode='range').",
                },
                "range_end": {
                    "type": "integer",
                    "description": "End character offset, exclusive (used with mode='range').",
                },
                "source": {
                    "type": "string",
                    "enum": ["auto", "html"],
                    "description": (
                        "Source override. 'auto' (default) uses the cache or best available "
                        "source. 'html' forces a fresh fetch of the publisher's article page "
                        "via the stealth browser, bypassing the PDF cache — use this when "
                        "the cached PDF has poor section structure (≤2 sections)."
                    ),
                    "default": "auto",
                },
            },
        },
    ),
    Tool(
        name="search_and_read",
        description=(
            "Combined: search for papers, then immediately fetch the FULL TEXT of "
            "the best match. Returns the complete article — often 50,000+ characters.\n\n"
            "Best for: short papers, or when you need a complete overview.\n\n"
            "For targeted questions about longer papers, prefer this workflow instead:\n"
            "  search_papers → batch_sections(dois=[...]) → search_in_article or "
            "  fetch_fulltext(mode='section').\n"
            "This avoids loading the entire text into context.\n\n"
            "QUERY TIPS: Decompose questions into 2-6 keywords. "
            "Example: 'CRISPR gene editing advances' not 'What are the latest "
            "advances in CRISPR gene editing?'"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "result_index": {
                    "type": "integer",
                    "description": "Which search result to fetch (0-indexed, default 0)",
                    "default": 0,
                },
                "use_proxy": {
                    "type": "boolean",
                    "description": "Route through institutional proxy",
                    "default": False,
                },
                "semantic": {
                    "type": "boolean",
                    "description": (
                        "Include semantic Zotero hits in the candidate list. "
                        "Defaults to SEMANTIC_DEFAULT_ON (true)."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="find_pdf_urls",
        description=(
            "List all available PDF URLs for a paper without downloading. "
            "Use this to check whether a paper is accessible before fetching, "
            "or to diagnose why fetch_fulltext failed for a particular DOI."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "DOI of the paper"},
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="search_zotero",
        description=(
            "Search your Zotero library (user library + ALL group libraries). "
            "When SQLite access is configured, this searches title, authors, "
            "abstract, tags, DOI, and fulltext across every library. "
            "Use search_papers with source='all' for broader cross-database search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="search_by_doi",
        description=(
            "Look up a paper in your Zotero library by DOI. Instant lookup "
            "when SQLite is configured — searches across ALL libraries "
            "including groups. The Zotero API cannot search by DOI, so "
            "this tool is critical for DOI-based workflows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI to search for (e.g. '10.1234/example')",
                },
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="list_zotero_libraries",
        description=(
            "List all Zotero libraries (your personal library and all group "
            "libraries) with item counts. Requires SQLite access."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="refresh_zotero_index",
        description=(
            "Rebuild the DOI index from your Zotero library and test all connections. "
            "Run after adding new papers to Zotero. Shows status of all backends: "
            "SQLite (preferred), local API, web API, and WebDAV."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="zotero_import_status",
        description=(
            "Inspect recent Zotero auto-import attempts, startup write probe state, "
            "and persistent queue depth. Use this to diagnose silent import failures."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent attempts to show (default 20, max 50).",
                    "default": 20,
                },
            },
        },
    ),
    Tool(
        name="scite_enrich",
        description=(
            "Fetch Scite citation intelligence for a DOI: supporting/contrasting/"
            "mentioning tallies, total citations, and retraction/correction signal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "Paper DOI."},
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="scite_check_retractions",
        description=(
            "Scan Zotero items (via SQLite shadow DB) for Scite retraction/correction "
            "notices. Optionally filter by collection key/name fragment."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Optional Zotero collection key or name fragment.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max Zotero DOI items to scan (default 200, max 2000).",
                    "default": 200,
                },
            },
        },
    ),
    Tool(
        name="semantic_search_zotero",
        description=(
            "Semantic search over your Zotero library using the ChromaDB embedding index.\n\n"
            "Returns chunk-level hits (up to *k* unique items after reranking). "
            "Each result includes:\n"
            "  • item_key, doi, title, score, rerank_score, snippet\n"
            "  • char_start / char_end: character offsets into the PDF text cache;\n"
            "    pass these to fetch_fulltext(mode='range', ...) to read the exact passage.\n"
            "  • chunk_source: 'ft_cache' (PDF fulltext) or 'abstract'\n\n"
            "A cross-encoder reranker (BAAI/bge-reranker-v2-m3) rescores the candidate "
            "pool before returning results. If the model is not loaded yet, "
            "bi-encoder ranking is used as a fallback.\n\n"
            "IMPORTANT: If semantic_index_status reports in_progress=true, the index "
            "is still being built and recall is partial — items not yet embedded will "
            "not appear in results. Fall back to search_zotero for known-item lookups "
            "during a build."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query."},
                "k": {
                    "type": "integer",
                    "description": "Number of unique items to return (default 10, max 50).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="semantic_index_status",
        description="Show semantic index status: last sync, model, item count, and cache path.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="semantic_index_rebuild",
        description=(
            "Sync or rebuild the semantic index.\n\n"
            "**Normal use (resume/incremental sync):** call with no arguments. "
            "This continues from where the last sync left off and never deletes existing vectors. "
            "Returns immediately — the sync runs in the background. "
            "Use semantic_index_status to monitor progress.\n\n"
            "**Force full rebuild (DESTRUCTIVE):** wipes the entire ChromaDB collection and "
            "re-embeds all ~500k chunks from scratch. This takes several days on typical hardware. "
            "ONLY do this if the embedding model has changed or the index is corrupt. "
            "Requires setting `confirm_wipe` to the exact string 'YES_WIPE_THE_ENTIRE_INDEX' — "
            "this is intentionally hard to type so an LLM cannot trigger it accidentally.\n\n"
            "Optional `provider` and `model` override the defaults from "
            "SEMANTIC_PROVIDER / SEMANTIC_MODEL for this run. Switching provider "
            "ALWAYS requires a force rebuild.\n\n"
            "Providers: local (sentence-transformers, any model id), openai "
            "(needs OPENAI_API_KEY; set OPENAI_BASE_URL for a local llama-server), "
            "gemini (needs GEMINI_API_KEY). Vectors are always stored locally."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm_wipe": {
                    "type": "string",
                    "description": "Must be exactly 'YES_WIPE_THE_ENTIRE_INDEX' to perform a destructive full rebuild. Omit for normal incremental sync.",
                },
                "provider": {
                    "type": "string",
                    "description": "Embedding provider: local | openai | gemini. Defaults to SEMANTIC_PROVIDER.",
                },
                "model": {
                    "type": "string",
                    "description": "Arbitrary model id for the chosen provider. Defaults to SEMANTIC_MODEL.",
                },
                "fulltext": {
                    "type": "boolean",
                    "description": "Deprecated — has no effect. Chunk-level sync always reads ft-cache.",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="search_in_article",
        description=(
            "Search within a cached article for specific terms or concepts. "
            "Returns a distribution heatmap showing WHERE each term concentrates "
            "across the paper, plus ranked text snippets with context and section "
            "attribution.\n\n"
            "Often the fastest way to answer a specific question — more efficient "
            "than reading full text or even whole sections. Works well even when "
            "section detection is poor.\n\n"
            "TIPS:\n"
            "- Use 2-5 varied terms with synonyms and abbreviations "
            "(e.g. 'algorithmic bias', 'discrimination', 'fairness', 'GDPR').\n"
            "- Multi-word phrases work: 'due diligence', 'surveillance capitalism'.\n"
            "- Follow up with fetch_fulltext(mode='range') using character offsets "
            "from results to read broader context around a match.\n\n"
            "The article must have been fetched previously via fetch_fulltext, "
            "batch_sections, or search_and_read."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the article to search within",
                },
                "terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "1-5 search terms or short phrases. Each is searched "
                        "independently. Use varied phrasings for best coverage."
                    ),
                },
                "context_chars": {
                    "type": "integer",
                    "description": "Characters of context around each match (default 500, max 2000)",
                    "default": 500,
                },
                "max_matches_per_term": {
                    "type": "integer",
                    "description": "Maximum matches to return per term (default 3, max 10)",
                    "default": 3,
                },
            },
            "required": ["doi", "terms"],
        },
    ),
    Tool(
        name="batch_sections",
        description=(
            "Get section listings (with keywords) for multiple papers in one call. "
            "Uncached papers are automatically fetched in parallel — so this is also "
            "the fastest way to fetch and survey a batch of papers from search results.\n\n"
            "Returns a combined overview showing the structure, headings, and topic "
            "keywords for each paper. Much faster than calling fetch_fulltext "
            "repeatedly.\n\n"
            "Typical workflow: search_papers → batch_sections(dois=[...from results...]) "
            "→ read the sections listings → fetch_fulltext(mode='section') or "
            "search_in_article on the papers and sections you care about."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dois": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DOIs to get sections for (up to 10)",
                },
                "use_proxy": {
                    "type": "boolean",
                    "description": "Route uncached fetches through institutional proxy",
                    "default": False,
                },
            },
            "required": ["dois"],
        },
    ),
    Tool(
        name="batch_search",
        description=(
            "Search for terms across multiple cached papers simultaneously. "
            "Returns a compact summary showing which papers mention which terms "
            "and where they concentrate — helping you quickly identify the most "
            "relevant paper for a specific concept before drilling in.\n\n"
            "Papers must have been fetched previously (use batch_sections to "
            "fetch and survey them first). Returns match counts and concentration "
            "patterns, not full snippets — use search_in_article on individual "
            "papers for detailed passages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dois": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DOIs to search across (up to 10)",
                },
                "terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-5 search terms or short phrases",
                },
            },
            "required": ["dois", "terms"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "search_papers":
            return await _handle_search(arguments)
        elif name == "get_paper":
            return await _handle_get_paper(arguments)
        elif name == "get_citations":
            return await _handle_get_citations(arguments)
        elif name == "get_references":
            return await _handle_get_references(arguments)
        elif name == "get_citation_tree":
            return await _handle_get_citation_tree(arguments)
        elif name == "get_book_chapters":
            return await _handle_get_book_chapters(arguments)
        elif name in ("fetch_fulltext", "fetch_pdf"):
            return await _handle_fetch_pdf(arguments)
        elif name == "search_and_read":
            return await _handle_search_and_read(arguments)
        elif name == "find_pdf_urls":
            return await _handle_find_pdf_urls(arguments)
        elif name == "search_zotero":
            return await _handle_search_zotero(arguments)
        elif name == "search_by_doi":
            return await _handle_search_by_doi(arguments)
        elif name == "list_zotero_libraries":
            return await _handle_list_libraries(arguments)
        elif name == "refresh_zotero_index":
            return await _handle_refresh_zotero_index(arguments)
        elif name == "zotero_import_status":
            return await _handle_zotero_import_status(arguments)
        elif name == "scite_enrich":
            return await _handle_scite_enrich(arguments)
        elif name == "scite_check_retractions":
            return await _handle_scite_check_retractions(arguments)
        elif name == "semantic_search_zotero":
            return await _handle_semantic_search_zotero(arguments)
        elif name == "semantic_index_status":
            return await _handle_semantic_index_status(arguments)
        elif name == "semantic_index_rebuild":
            return await _handle_semantic_index_rebuild(arguments)
        elif name == "search_in_article":
            return await _handle_search_in_article(arguments)
        elif name == "batch_sections":
            return await _handle_batch_sections(arguments)
        elif name == "batch_search":
            return await _handle_batch_search(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def _handle_zotero_import_status(args: dict) -> list[TextContent]:
    limit = max(1, min(int(args.get("limit", 20)), 50))
    status = await zotero_import.get_import_status(limit=limit)

    lines = [
        "Zotero auto-import status",
        "=" * 40,
        f"Enabled: {status.get('auto_import_enabled')}",
        f"Local API enabled: {status.get('local_api_enabled')}",
        f"Queue depth: {status.get('queue_count')}",
    ]

    probe = status.get("write_probe") or {}
    lines.append(
        "Write probe: "
        f"state={probe.get('state')}"
        f", status={probe.get('status_code')}"
        f", checked_at={probe.get('checked_at')}"
    )
    if probe.get("message"):
        lines.append(f"Probe detail: {probe['message']}")

    attempts = status.get("recent_attempts") or []
    lines.append("\nRecent attempts:")
    if not attempts:
        lines.append("  (none)")
    else:
        for a in attempts[-limit:]:
            err = f" | error={a.get('error')}" if a.get("error") else ""
            lines.append(
                f"  - {a.get('timestamp')} | {a.get('doi')} | {a.get('stage')} | {a.get('status')}{err}"
            )

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_scite_enrich(args: dict) -> list[TextContent]:
    doi = (args.get("doi") or "").strip()
    if not doi:
        return [TextContent(type="text", text="scite_enrich requires a DOI.")]

    tallies = await scite.get_scite_tallies(doi)
    papers = await scite.get_scite_papers_batch([doi])
    paper = papers.get(zotero._normalize_doi(doi)) or papers.get(doi) or {}
    retracted = scite.paper_has_retraction_notice(paper)

    if not tallies and not paper:
        return [TextContent(type="text", text=f"No Scite data found for DOI: {doi}")]

    lines = [
        f"Scite for DOI: {doi}",
        "=" * 40,
    ]
    if tallies:
        lines.append(f"Citing publications: {tallies.get('citing', 0)}")
        lines.append(f"Supporting: {tallies.get('supporting', 0)}")
        lines.append(f"Contrasting: {tallies.get('contrasting', 0)}")
        lines.append(f"Mentioning: {tallies.get('mentioning', 0)}")
        lines.append(f"Total statements: {tallies.get('total', 0)}")
    lines.append(f"Retraction/correction signal: {retracted}")

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_scite_check_retractions(args: dict) -> list[TextContent]:
    limit = max(1, min(int(args.get("limit", 200)), 2000))
    collection = (args.get("collection") or "").strip() or None

    items = await zotero_sqlite.list_items_with_doi(limit=limit, collection=collection)
    if not items:
        scope = f" in collection '{collection}'" if collection else ""
        return [TextContent(type="text", text=f"No DOI-bearing Zotero items found{scope}.")]

    doi_to_item: dict[str, dict] = {}
    for it in items:
        doi = zotero._normalize_doi(it.get("doi") or "")
        if doi:
            doi_to_item[doi] = it

    papers = await scite.get_scite_papers_batch(list(doi_to_item.keys()))
    flagged: list[dict] = []
    for doi, paper in papers.items():
        if scite.paper_has_retraction_notice(paper):
            it = doi_to_item.get(zotero._normalize_doi(doi))
            if it:
                flagged.append({
                    "doi": it.get("doi"),
                    "item_key": it.get("item_key"),
                    "title": it.get("title") or "(untitled)",
                })

    lines = [
        f"Scanned {len(doi_to_item)} DOI-bearing Zotero items.",
        f"Flagged by Scite retraction/correction notices: {len(flagged)}",
    ]
    if flagged:
        lines.append("=" * 40)
        for row in flagged[:200]:
            lines.append(f"- {row['title']}")
            lines.append(f"  DOI: {row['doi']}")
            lines.append(f"  Zotero key: {row['item_key']}")

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_semantic_search_zotero(args: dict) -> list[TextContent]:
    query = (args.get("query") or "").strip()
    k = max(1, min(int(args.get("k", 10)), 50))
    if not query:
        return [TextContent(type="text", text="semantic_search_zotero requires a query.")]

    try:
        from .semantic_index import SemanticIndexUnavailable, get_semantic_index
        from .cross_reranker import rerank
        from .config import config as _config
    except ImportError:
        from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index
        from academic_mcp.cross_reranker import rerank
        from academic_mcp.config import config as _config

    try:
        _ensure_semantic_background_sync()
        idx = get_semantic_index()
        # Over-fetch by cross_reranker_fetch candidates for the reranker.
        fetch_n = max(k, _config.cross_reranker_fetch or 50)
        chunks = await idx.search(query, k=fetch_n)
    except SemanticIndexUnavailable as e:
        return [TextContent(type="text", text=str(e))]

    if not chunks:
        return [TextContent(type="text", text="No semantic hits found. Build the index with semantic_index_rebuild first.")]

    # Rerank the candidate pool and keep top-k unique items.
    reranked = await rerank(query, chunks, top_k=len(chunks))

    # Deduplicate by item_key, keeping highest-scoring chunk per item.
    seen_keys: set[str] = set()
    unique_hits: list[dict] = []
    for h in reranked:
        ik = h.get("item_key") or ""
        if ik not in seen_keys:
            seen_keys.add(ik)
            unique_hits.append(h)
        if len(unique_hits) >= k:
            break

    lines = [f"Semantic Zotero hits for: {query}", "=" * 50]
    for i, h in enumerate(unique_hits, start=1):
        lines.append(f"[{i}] {h.get('title') or '(untitled)'}")
        score_str = f"score={h.get('score', 0):.4f}"
        if "rerank_score" in h:
            score_str += f" | rerank={h['rerank_score']:.4f}"
        lines.append(f"    key={h.get('item_key')} | {score_str}")
        if h.get("doi"):
            lines.append(f"    DOI: {h['doi']}")
        if h.get("chunk_source") == "ft_cache":
            lines.append(
                f"    chunk: chars {h.get('char_start', 0)}–{h.get('char_end', 0)} "
                f"(chunk {h.get('chunk_idx', 0)+1}/{h.get('chunk_count', 1)})"
            )
            lines.append(
                f"    → fetch_fulltext(doi='{h.get('doi', '')}', mode='range', "
                f"start={h.get('char_start', 0)}, end={h.get('char_end', 0)})"
            )
        if h.get("snippet"):
            lines.append(f"    {h['snippet'][:220]}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_semantic_index_status(args: dict) -> list[TextContent]:
    try:
        from .semantic_index import SemanticIndexUnavailable, get_semantic_index
    except ImportError:
        from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index

    try:
        status = await get_semantic_index().status()
    except SemanticIndexUnavailable as e:
        return [TextContent(type="text", text=str(e))]

    # Mirror freshness watermark — written by zotero-sync.sh after each
    # successful rsync.  Present only in the networked Asahi deployment.
    try:
        import time as _time
        from pathlib import Path as _Path
        watermark = _Path(zotero_sqlite.sqlite_config.db_path).parent / ".last-sync"
        if watermark.exists():
            status["mirror_last_sync_utc"] = watermark.read_text().strip()
            status["mirror_age_seconds"] = int(_time.time() - watermark.stat().st_mtime)
    except Exception:
        pass

    return [TextContent(type="text", text=json.dumps(status, indent=2))]


async def _handle_semantic_index_rebuild(args: dict) -> list[TextContent]:
    confirm_wipe = args.get("confirm_wipe", "")
    force_rebuild = confirm_wipe == "YES_WIPE_THE_ENTIRE_INDEX"
    provider = args.get("provider") or None
    model = args.get("model") or None

    if args.get("confirm_wipe") and not force_rebuild:
        return [TextContent(type="text", text=(
            "confirm_wipe value did not match. To perform a full destructive rebuild, "
            "set confirm_wipe to exactly 'YES_WIPE_THE_ENTIRE_INDEX'. "
            "To resume an incremental sync, call with no arguments."
        ))]

    try:
        from .semantic_index import SemanticIndexUnavailable, get_semantic_index
    except ImportError:
        from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index

    global _semantic_sync_task, _semantic_empty_hint_shown
    _semantic_empty_hint_shown = False

    if _semantic_sync_task and not _semantic_sync_task.done():
        if force_rebuild:
            _semantic_sync_task.cancel()
        else:
            return [TextContent(type="text", text=(
                "A sync is already running in the background. "
                "Use semantic_index_status to check progress."
            ))]

    async def _runner() -> None:
        try:
            await get_semantic_index().sync(
                force_rebuild=force_rebuild,
                include_fulltext=False,
                provider=provider,
                model=model,
            )
        except SemanticIndexUnavailable as e:
            logger.warning("semantic_index_rebuild background task: %s", e)
        except Exception as e:
            logger.exception("semantic_index_rebuild background task failed: %s", e)

    _semantic_sync_task = asyncio.create_task(_runner())

    if force_rebuild:
        msg = (
            "Full index wipe started in the background. "
            "All existing vectors have been deleted and re-embedding has begun. "
            "This will take several days. Use semantic_index_status to monitor progress."
        )
    else:
        msg = (
            "Incremental sync started in the background. "
            "New and updated chunks will be embedded; existing vectors are preserved. "
            "Use semantic_index_status to monitor progress."
        )
    return [TextContent(type="text", text=msg)]


async def _collect_search_results(args: dict) -> list[dict]:
    """Run the unified parallel-search pipeline and return merged, reranked results.

    Used by search_papers (which formats the output) and search_and_read
    (which picks a single result to fetch).
    """
    query = args["query"]
    limit = min(args.get("limit", 5), 20)
    source = args.get("source", "all")
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    venue = args.get("venue")
    domain_hint = args.get("domain_hint", "general")
    include_scite = bool(args.get("include_scite", False))
    # `semantic` defaults to config.semantic_default_on; explicit per-call wins.
    if "semantic" in args and args["semantic"] is not None:
        use_semantic = bool(args["semantic"])
    else:
        use_semantic = config.semantic_default_on

    # Pre-fetch DOI index once (used to flag Zotero membership in API results).
    zot_index = await zotero.get_doi_index()

    # ── Per-source fetchers ─────────────────────────────────────────
    # Each fetcher returns a list of normalized result dicts. They are
    # scheduled in parallel and merged in priority order below.

    async def fetch_zotero_lex() -> list[dict]:
        out: list[dict] = []
        zot_results = await zotero.search_zotero(
            query, limit=limit,
            start_year=start_year, end_year=end_year,
        )
        for item in zot_results:
            creators = item.get("creators", [])
            author_names = []
            for c in (creators if isinstance(creators, list) else []):
                if isinstance(c, dict):
                    name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                    if name:
                        author_names.append(name)
            doi = (item.get("DOI") or "").strip()
            out.append({
                "title": item.get("title") or "Untitled",
                "authors": author_names,
                "year": (item.get("date") or "")[:4] or None,
                "doi": doi or None,
                "zotero_key": item.get("key") or None,
                "abstract": (item.get("abstractNote") or "").strip() or None,
                "citations": None,
                "venue": item.get("publicationTitle") or None,
                "found_in": ["zotero"],
                "in_zotero": True,
                "has_oa_pdf": True,
                "s2_id": None,
                "url": (item.get("url") or "").strip() or None,
            })
        return out

    async def fetch_semantic_zotero() -> list[dict]:
        # Empty-index / unavailable → return [] (and surface a one-time hint
        # via _semantic_index_status_for_hint, set below).
        try:
            from .semantic_index import SemanticIndexUnavailable, get_semantic_index
            from .cross_reranker import rerank as _cross_rerank
        except ImportError:
            from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index
            from academic_mcp.cross_reranker import rerank as _cross_rerank

        _ensure_semantic_background_sync()
        try:
            idx = get_semantic_index()
            # Skip cleanly if the index is empty — the one-time hint is
            # appended in the formatter using the index status.
            try:
                _st = await idx.status()
                if int(_st.get("count") or 0) <= 0:
                    return []
            except Exception:
                pass
            fetch_n = max(limit, config.cross_reranker_fetch or 50)
            chunks = await idx.search(query, k=fetch_n)
        except SemanticIndexUnavailable:
            return []
        except Exception as e:
            logger.warning("Semantic Zotero search failed: %s", e)
            return []

        if not chunks:
            return []

        # Rerank candidate pool, then dedupe by item_key keeping best chunk.
        try:
            reranked = await _cross_rerank(query, chunks, top_k=len(chunks))
        except Exception as e:
            logger.warning("Cross-reranker failed, falling back to bi-encoder order: %s", e)
            reranked = chunks

        seen_keys: set[str] = set()
        unique_hits: list[dict] = []
        for h in reranked:
            ik = h.get("item_key") or ""
            if ik and ik not in seen_keys:
                seen_keys.add(ik)
                unique_hits.append(h)
            if len(unique_hits) >= limit:
                break

        out: list[dict] = []
        for hit in unique_hits:
            key = hit.get("item_key")
            if not key:
                continue
            item = await zotero_sqlite.search_by_key(key)
            if not item:
                continue
            author_names = []
            for c in (item.creators or []):
                nm = c.display_name.strip()
                if nm:
                    author_names.append(nm)
            score = hit.get("rerank_score", hit.get("score"))
            out.append({
                "title": item.title or hit.get("title") or "Untitled",
                "authors": author_names,
                "year": (item.date or "")[:4] or None,
                "doi": item.DOI or hit.get("doi") or None,
                "zotero_key": item.key,
                "abstract": item.abstractNote or hit.get("snippet") or None,
                "citations": None,
                "venue": item.publicationTitle or None,
                "found_in": ["semantic_zotero", "zotero"],
                "in_zotero": True,
                "has_oa_pdf": True,
                "s2_id": None,
                "_semantic_zotero_score": score,
                "url": (item.url or "").strip() or None,
            })
        return out

    async def fetch_s2() -> list[dict]:
        out: list[dict] = []
        s2 = await apis.s2_search(
            query, limit=limit,
            start_year=start_year, end_year=end_year,
        )
        for paper in s2.get("data", []):
            doi = apis.extract_doi(paper)
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            out.append({
                "title": paper.get("title") or "Untitled",
                "authors": [a.get("name", "") for a in (paper.get("authors") or [])[:5]],
                "year": paper.get("year"),
                "doi": doi,
                "abstract": (paper.get("abstract") or "").strip() or None,
                "citations": paper.get("citationCount"),
                "venue": paper.get("venue") or None,
                "found_in": ["semantic_scholar"],
                "in_zotero": in_zot,
                "has_oa_pdf": bool((paper.get("openAccessPdf") or {}).get("url")),
                "s2_id": paper.get("paperId"),
            })
        return out

    async def fetch_openalex() -> list[dict]:
        out: list[dict] = []
        oa = await apis.openalex_search(
            query, limit=limit,
            start_year=start_year, end_year=end_year, venue=venue,
        )
        for work in oa.get("results", []):
            doi = apis.extract_doi(work)
            doi_norm = zotero._normalize_doi(doi) if doi else None
            authors: list[str] = []
            for auth in (work.get("authorships") or []):
                if not auth:
                    continue
                name = (auth.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
                if len(authors) >= 5:
                    break
            in_zot = doi_norm in zot_index if doi_norm else False
            _primary_loc = work.get("primary_location") or {}
            _oa_source = _primary_loc.get("source") or {}
            _oa_type = (work.get("type") or "").lower()
            _oa_url = _primary_loc.get("pdf_url") or _primary_loc.get("landing_page_url") or None
            out.append({
                "title": work.get("title") or "Untitled",
                "authors": authors,
                "year": work.get("publication_year"),
                "doi": doi,
                "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")) or None,
                "citations": work.get("cited_by_count"),
                "venue": _oa_source.get("display_name") or None,
                "found_in": ["openalex"],
                "in_zotero": in_zot,
                "has_oa_pdf": (work.get("open_access") or {}).get("is_oa", False),
                "s2_id": None,
                "work_type": _oa_type or None,
                "container_title": _oa_source.get("display_name") if _oa_type in ("book-chapter",) else None,
                "url": _oa_url,
            })
        return out

    async def fetch_primo() -> list[dict]:
        primo_results = await apis.primo_search(
            query, limit=limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[dict] = []
        for r in primo_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            r["in_zotero"] = doi_norm in zot_index if doi_norm else False
            out.append(r)
        return out

    async def fetch_primo_law() -> list[dict]:
        law_results = await apis.primo_search_law_reviews(
            query, limit=limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[dict] = []
        for r in law_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            if in_zot:
                r["in_zotero"] = True
                r["has_oa_pdf"] = True
            else:
                r["in_zotero"] = False
            out.append(r)
        return out

    # ── Schedule fetchers in parallel ───────────────────────────────
    tasks: dict[str, "asyncio.Future"] = {}
    if source in ("all", "zotero"):
        tasks["zotero"] = asyncio.ensure_future(fetch_zotero_lex())
    if use_semantic and source in ("all", "semantic_zotero"):
        tasks["semantic_zotero"] = asyncio.ensure_future(fetch_semantic_zotero())
    if source in ("all", "semantic_scholar"):
        tasks["semantic_scholar"] = asyncio.ensure_future(fetch_s2())
    if source in ("all", "openalex"):
        tasks["openalex"] = asyncio.ensure_future(fetch_openalex())
    if source in ("all", "primo") and (config.primo_domain and config.primo_vid):
        tasks["primo"] = asyncio.ensure_future(fetch_primo())
    if (
        domain_hint == "law"
        and source in ("all", "primo")
        and (config.primo_domain and config.primo_vid)
    ):
        tasks["primo_law"] = asyncio.ensure_future(fetch_primo_law())

    if tasks:
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        by_source: dict[str, list[dict]] = {}
        for src_name, res in zip(tasks.keys(), gathered):
            if isinstance(res, Exception):
                logger.warning("%s search failed: %s", src_name, res)
                by_source[src_name] = []
            else:
                by_source[src_name] = res
    else:
        by_source = {}

    # ── Merge results in priority order ─────────────────────────────
    # Earlier sources win the "primary" record; later ones enrich found_in /
    # missing fields. Order: Zotero lexical → semantic Zotero (so Zotero items
    # surface first) → S2 → OpenAlex → Primo → Primo law.
    priority = [
        "zotero",
        "semantic_zotero",
        "semantic_scholar",
        "openalex",
        "primo",
        "primo_law",
    ]

    results: list[dict] = []
    seen_dois: set[str] = set()
    seen_zot_keys: set[str] = set()

    def _find_existing(rec: dict) -> dict | None:
        d = rec.get("doi")
        dn = zotero._normalize_doi(d) if d else None
        zk = rec.get("zotero_key")
        if dn and dn in seen_dois:
            for r in results:
                if r.get("doi") and zotero._normalize_doi(r["doi"]) == dn:
                    return r
        if zk and zk in seen_zot_keys:
            for r in results:
                if r.get("zotero_key") == zk:
                    return r
        return None

    def _merge_into(existing: dict, rec: dict) -> None:
        for s in rec.get("found_in", []):
            if s not in existing["found_in"]:
                existing["found_in"].append(s)
        if not existing.get("citations") and rec.get("citations"):
            existing["citations"] = rec["citations"]
        if not existing.get("abstract") and rec.get("abstract"):
            existing["abstract"] = rec["abstract"]
        if not existing.get("s2_id") and rec.get("s2_id"):
            existing["s2_id"] = rec["s2_id"]
        if not existing.get("url") and rec.get("url"):
            existing["url"] = rec["url"]
        if not existing.get("venue") and rec.get("venue"):
            existing["venue"] = rec["venue"]
        if not existing.get("work_type") and rec.get("work_type"):
            existing["work_type"] = rec["work_type"]
        if not existing.get("container_title") and rec.get("container_title"):
            existing["container_title"] = rec["container_title"]
        if rec.get("_primo_proxy_url") and not existing.get("_primo_proxy_url"):
            existing["_primo_proxy_url"] = rec["_primo_proxy_url"]
        if rec.get("_primo_oa_url") and not existing.get("_primo_oa_url"):
            existing["_primo_oa_url"] = rec["_primo_oa_url"]
            existing["has_oa_pdf"] = existing.get("has_oa_pdf") or rec.get("has_oa_pdf", False)
        if rec.get("_semantic_zotero_score") is not None and existing.get("_semantic_zotero_score") is None:
            existing["_semantic_zotero_score"] = rec["_semantic_zotero_score"]
        if rec.get("in_zotero") and not existing.get("in_zotero"):
            existing["in_zotero"] = True

    for src_name in priority:
        for rec in by_source.get(src_name, []):
            existing = _find_existing(rec)
            if existing is not None:
                _merge_into(existing, rec)
                continue
            d = rec.get("doi")
            dn = zotero._normalize_doi(d) if d else None
            if dn:
                seen_dois.add(dn)
            zk = rec.get("zotero_key")
            if zk:
                seen_zot_keys.add(zk)
            results.append(rec)

    # ── 5. For Zotero items without abstracts, try getting a preview ──
    for r in results:
        if r["in_zotero"] and not r["abstract"] and r.get("doi"):
            try:
                zot_result = await zotero.get_paper_from_zotero(r["doi"])
                if zot_result and zot_result.get("text"):
                    # Use first ~600 chars of fulltext as preview
                    preview = zot_result["text"][:600].strip()
                    # Try to cut at a sentence boundary
                    last_period = preview.rfind(".")
                    if last_period > 300:
                        preview = preview[:last_period + 1]
                    r["abstract"] = f"[Preview from Zotero fulltext]: {preview}"
                elif zot_result and zot_result.get("pdf_path"):
                    # Extract first page as preview
                    page1 = pdf_extractor.extract_text_by_pages(zot_result["pdf_path"], 1, 1)
                    if page1.strip():
                        preview = page1.strip()[:600]
                        last_period = preview.rfind(".")
                        if last_period > 200:
                            preview = preview[:last_period + 1]
                        r["abstract"] = f"[Preview from PDF page 1]: {preview}"
            except Exception as e:
                logger.debug("Preview extraction failed for %s: %s", r["doi"], e)

    # ── 6. Semantic re-ranking ─────────────────────────────────────
    #
    # Use sentence-transformers (all-MiniLM-L6-v2) to compute cosine
    # similarity between the query and each result's abstract/title.
    # Zotero items still get a priority boost, but within each tier
    # results are ordered by true semantic relevance.
    # Falls back to composite scoring if the model is unavailable.
    results = await rerank_results(query, results)
    return results


async def _handle_search(args: dict) -> list[TextContent]:
    global _semantic_empty_hint_shown

    query = args["query"]
    include_scite = bool(args.get("include_scite", False))
    if "semantic" in args and args["semantic"] is not None:
        use_semantic = bool(args["semantic"])
    else:
        use_semantic = config.semantic_default_on

    def _authors_str(authors: list) -> str:
        if not authors:
            return "Unknown"
        names = authors[:3]
        s = ", ".join(names)
        if len(authors) > 3:
            s += f" +{len(authors)-3} more"
        return s

    results = await _collect_search_results(args)


    # ── 7. Optional Scite enrichment ─────────────────────────────────
    if include_scite:
        dois = [zotero._normalize_doi(r["doi"]) for r in results if r.get("doi")]
        if dois:
            tallies_by_doi = await scite.get_scite_tallies_batch(dois)
            papers_by_doi = await scite.get_scite_papers_batch(dois)

            for r in results:
                doi = r.get("doi")
                if not doi:
                    continue
                doi_norm = zotero._normalize_doi(doi)
                tally = tallies_by_doi.get(doi_norm)
                paper = papers_by_doi.get(doi_norm) or papers_by_doi.get(doi)
                is_retracted = scite.paper_has_retraction_notice(paper)
                if tally:
                    tally = dict(tally)
                    tally["retracted"] = is_retracted
                    r["scite"] = tally
                elif is_retracted:
                    r["scite"] = {
                        "supporting": 0,
                        "contrasting": 0,
                        "mentioning": 0,
                        "citing": 0,
                        "total": 0,
                        "retracted": True,
                    }

            # Conservative ranking adjustment: retractions sink; strong support ratio nudges up.
            def _scite_adjust(rr: dict) -> float:
                s = rr.get("scite") or {}
                if not s:
                    return 0.0
                if s.get("retracted"):
                    return -0.25
                citing = max(1, int(s.get("citing") or 0))
                supporting = int(s.get("supporting") or 0)
                return min(0.08, (supporting / citing) * 0.08)

            for r in results:
                r["_scite_adjust"] = _scite_adjust(r)

            results.sort(
                key=lambda r: (
                    0 if (r.get("scite") or {}).get("retracted") else 1,
                    1 if r.get("in_zotero") else 0,
                    (r.get("_semantic_similarity") or 0.0) + (r.get("_scite_adjust") or 0.0),
                    r.get("citations") or 0,
                ),
                reverse=True,
            )

    # ── 8. Format output as RAG-friendly text ────────────────────────
    if not results:
        return [TextContent(type="text", text=f"No papers found for '{query}'.")]

    text = f"Found {len(results)} papers for '{query}':\n"

    # Enrichment status block — shown when caller requested scite or semantic.
    if include_scite or use_semantic:
        _status_parts: list[str] = []
        if include_scite:
            _with_doi = sum(1 for r in results if r.get("doi"))
            _enriched = sum(1 for r in results if r.get("scite"))
            _status_parts.append(f"scite: {_enriched}/{_with_doi} enriched")
        _sem_empty_hint = ""
        if use_semantic:
            try:
                from .semantic_index import get_semantic_index
            except ImportError:
                from academic_mcp.semantic_index import get_semantic_index
            try:
                _sem_st = get_semantic_index()._load_status()
                _sem_count = int(_sem_st.get("count") or 0)
                # One-time hint when the user has semantic on but nothing indexed.
                if _sem_count <= 0 and not _sem_st.get("in_progress") and not _semantic_empty_hint_shown:
                    _sem_empty_hint = (
                        "\nTip: semantic Zotero search is enabled but your index is empty — "
                        "run `semantic_index_rebuild` to populate it. "
                        "(Disable globally with SEMANTIC_DEFAULT_ON=false, or pass semantic=false.)\n"
                    )
                    _semantic_empty_hint_shown = True
                if _sem_st.get("in_progress"):
                    _upserted = int(_sem_st.get("upserted") or 0)
                    _pending = int(_sem_st.get("pending") or 0)
                    _total = _upserted + _pending
                    _pct = (100 * _upserted / _total) if _total else 0
                    _age = (
                        f"build in progress — "
                        f"{_upserted:,}/{_total:,} chunks ({_pct:.1f}%) embedded"
                    )
                else:
                    _last_sync = _sem_st.get("last_sync") or ""
                    if _last_sync:
                        from datetime import datetime as _dt2, timezone as _tz2
                        _delta = _dt2.now(_tz2.utc) - _dt2.fromisoformat(_last_sync)
                        _h = int(_delta.total_seconds() // 3600)
                        if _h < 1:
                            _age = f"synced {int(_delta.total_seconds() // 60)}m ago"
                        elif _h < 24:
                            _age = f"synced {_h}h ago"
                        else:
                            _age = f"synced {int(_h // 24)}d ago"
                    else:
                        _age = "not synced yet — run semantic_index_rebuild"
                _status_parts.append(f"semantic: {_sem_count:,} items, {_age}")
            except Exception:
                _status_parts.append("semantic: unavailable")
        if _status_parts:
            text += f"[{' | '.join(_status_parts)}]\n"
        if _sem_empty_hint:
            text += _sem_empty_hint

    text += "=" * 60 + "\n\n"

    from datetime import datetime as _dt
    _current_year = _dt.now().year

    for i, r in enumerate(results):
        # Header line with index, title, and availability badges
        badges = []
        if r["in_zotero"]:
            badges.append("★ IN ZOTERO")
        if r["has_oa_pdf"]:
            badges.append("OA")
        if (r.get("scite") or {}).get("retracted"):
            badges.append("RETRACTED?")
        _wt = (r.get("work_type") or "").lower()
        if _wt == "book-chapter":
            badges.append("CHAPTER")
        elif _wt in ("book", "edited-book", "monograph", "reference-book"):
            badges.append("BOOK")
        badge_str = f"  [{', '.join(badges)}]" if badges else ""
        text += f"[{i}] {r['title']}{badge_str}\n"

        # Metadata
        text += f"    Authors: {_authors_str(r['authors'])}\n"
        if r.get("year"):
            text += f"    Year: {r['year']}"
            if r.get("citations"):
                text += f"  |  Citations: {r['citations']}"
            text += "\n"
        if r.get("venue"):
            law_note = "  [law review — via Primo/HeinOnline]" if "primo_law" in r["found_in"] else ""
            text += f"    Venue: {r['venue']}{law_note}\n"
        if _wt == "book-chapter" and r.get("container_title") and r.get("container_title") != r.get("venue"):
            text += f"    In book: {r['container_title']}\n"
        if r.get("doi"):
            text += f"    DOI: {r['doi']}\n"
        text += f"    Sources: {', '.join(r['found_in'])}\n"
        if r.get("_semantic_similarity") is not None:
            text += f"    Relevance: {r['_semantic_similarity']:.3f}\n"
        if r.get("_semantic_zotero_score") is not None:
            text += f"    Semantic Zotero score: {r['_semantic_zotero_score']:.3f}\n"
        if r.get("scite"):
            s = r["scite"]
            text += (
                "    Scite: "
                f"citing={s.get('citing', 0)} | "
                f"supporting={s.get('supporting', 0)} | "
                f"contrasting={s.get('contrasting', 0)} | "
                f"mentioning={s.get('mentioning', 0)}"
            )
            if s.get("retracted"):
                text += "  [retraction/correction signal]"
            text += "\n"

        # Abstract / Preview
        abstract = r.get("abstract") or ""
        if abstract:
            # Truncate long abstracts for the listing
            if len(abstract) > 400:
                abstract = abstract[:400] + "..."
            text += f"\n    {abstract}\n"

        # URL line — shown only for DOI-less items so the LLM can pass it to
        # fetch_fulltext.  When a DOI is present it's already the canonical handle.
        if r.get("url") and not r.get("doi"):
            text += f"    URL: {r['url']}\n"

        # Follow-up action guidance
        text += "\n    → "
        if r.get("doi"):
            if r["in_zotero"]:
                text += f"Full text available. Call fetch_fulltext(doi=\"{r['doi']}\", mode=\"sections\") to explore."
            elif r.get("_primo_oa_url"):
                text += f"Open access via library. Call fetch_fulltext(doi=\"{r['doi']}\", mode=\"sections\") to explore."
            elif r["has_oa_pdf"]:
                text += f"Open access PDF available. Call fetch_fulltext(doi=\"{r['doi']}\", mode=\"sections\") to explore."
            elif r.get("_primo_proxy_url"):
                text += f"Available via institutional access: {r['_primo_proxy_url']}"
            else:
                text += f"May need proxy. Call fetch_fulltext(doi=\"{r['doi']}\", use_proxy=true, mode=\"sections\") to explore."
        elif r.get("url"):
            text += (
                f"No DOI, but URL available. "
                f"Call fetch_fulltext(url=\"{r['url']}\", mode=\"sections\") to explore."
            )
        elif r.get("in_zotero") and r.get("zotero_key"):
            text += (
                f"No DOI, but in Zotero. Call fetch_fulltext(zotero_key=\"{r['zotero_key']}\", "
                "mode=\"sections\") to explore."
            )
        else:
            text += "No DOI — full text retrieval not available for this result."

        # Expansion hint: for promising older papers, nudge toward citation-graph
        # exploration — often more productive than another keyword search.
        try:
            _yr = int(r.get("year")) if r.get("year") else None
        except (TypeError, ValueError):
            _yr = None
        _sim = r.get("_semantic_similarity")
        _cites = r.get("citations") or 0
        _looks_strong = (i == 0) or (isinstance(_sim, (int, float)) and _sim >= 0.3) or _cites >= 20
        if r.get("doi") and _yr and _yr <= _current_year - 1 and _looks_strong:
            text += (
                f"\n    ⇢ Promising + {_current_year - _yr}yr old: consider "
                f"get_citations(doi=\"{r['doi']}\") to see what built on it. "
                "Pass exclude_dois=[the DOIs below] to skip results you've already seen, "
                "and use BROADER keywords than this query to pull in adjacent work."
            )
        # Book/chapter navigation hints
        if r.get("doi") and _wt == "book-chapter":
            text += (
                f"\n    ⇢ Book chapter: get_book_chapters(doi=\"{r['doi']}\") lists "
                "the sibling chapters in the same volume — often a richer topical "
                "neighbourhood than keyword search."
            )
        elif r.get("doi") and _wt in ("book", "edited-book", "monograph", "reference-book"):
            text += (
                f"\n    ⇢ Book: get_book_chapters(doi=\"{r['doi']}\") drills down into "
                "the individual chapters (each has its own DOI and can be fetched separately). "
                "Prefer this to fetching the whole book — chapter PDFs are usually the only "
                "thing publishers host, and sectioning works much better per-chapter."
            )
        text += "\n\n"

    # Footer: ready-to-paste exclude_dois list for citation-based widening.
    _result_dois = [r["doi"] for r in results if r.get("doi")]
    if _result_dois:
        text += "─" * 60 + "\n"
        text += "To widen via citations without repeats, copy this list as exclude_dois:\n"
        text += json.dumps(_result_dois) + "\n"

    return [TextContent(type="text", text=text)]


async def _handle_get_paper(args: dict) -> list[TextContent]:
    identifier = args["identifier"]

    # Try as DOI first
    s2_paper = None
    oa_paper = None
    unpaywall_data = None

    try:
        s2_paper = await apis.s2_paper(f"DOI:{identifier}")
    except Exception:
        try:
            s2_paper = await apis.s2_paper(identifier)
        except Exception as e:
            logger.debug("S2 lookup failed: %s", e)

    try:
        oa_paper = await apis.openalex_work(identifier)
    except Exception as e:
        logger.debug("OpenAlex lookup failed: %s", e)

    doi = None
    if s2_paper:
        doi = apis.extract_doi(s2_paper)
    if not doi and oa_paper:
        doi = apis.extract_doi(oa_paper)
    if not doi:
        doi = identifier  # assume it's a DOI

    try:
        unpaywall_data = await apis.unpaywall_lookup(doi)
    except Exception as e:
        logger.debug("Unpaywall lookup failed: %s", e)

    # Build response
    paper = s2_paper or {}
    text = f"Paper: {paper.get('title') or (oa_paper or {}).get('title', 'Unknown')}\n"
    text += f"DOI: {doi}\n"

    if s2_paper:
        text += f"\nSemantic Scholar ID: {s2_paper.get('paperId')}\n"
        authors = [a.get("name") for a in (s2_paper.get("authors") or [])]
        text += f"Authors: {', '.join(authors)}\n"
        text += f"Year: {s2_paper.get('year')}\n"
        text += f"Venue: {s2_paper.get('venue')}\n"
        text += f"Citations: {s2_paper.get('citationCount')}\n"
        text += f"References: {s2_paper.get('referenceCount')}\n"

        tldr = s2_paper.get("tldr")
        if tldr:
            text += f"\nTL;DR: {tldr.get('text', '')}\n"

        if s2_paper.get("abstract"):
            text += f"\nAbstract:\n{s2_paper['abstract']}\n"

    elif oa_paper:
        text += f"\nAuthors: "
        authors = [a.get("author", {}).get("display_name", "") for a in (oa_paper.get("authorships") or [])[:10]]
        text += ", ".join(authors) + "\n"
        text += f"Year: {oa_paper.get('publication_year')}\n"
        text += f"Citations: {oa_paper.get('cited_by_count')}\n"

        abstract = _reconstruct_abstract(oa_paper.get("abstract_inverted_index"))
        if abstract:
            text += f"\nAbstract:\n{abstract}\n"

    # Book / chapter drill hint
    oa_type = ((oa_paper or {}).get("type") or "").lower()
    if oa_type in _CHAPTER_TYPES:
        container = ((oa_paper.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        text += "\nType: book chapter"
        if container:
            text += f" in \"{container}\""
        text += (
            f"\n→ See sibling chapters: get_book_chapters(doi=\"{doi}\")\n"
        )
    elif oa_type in _BOOK_TYPES:
        text += (
            "\nType: book\n"
            f"→ List chapters: get_book_chapters(doi=\"{doi}\") — fetch individual "
            "chapters rather than the whole book PDF.\n"
        )

    # PDF URLs
    pdf_urls = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)
    if pdf_urls:
        text += "\nAvailable PDF URLs:\n"
        for p in pdf_urls:
            text += f"  [{p['source']}] {p['url']}\n"
    else:
        text += "\nNo open access PDF URLs found.\n"
        if unpaywall_data and not unpaywall_data.get("is_oa"):
            text += "Paper appears to be behind a paywall. Try with use_proxy=true.\n"

    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Citation graph tool helpers & handlers
# ---------------------------------------------------------------------------

def _normalize_exclude_dois(exclude_dois: list[str] | None) -> set[str]:
    """Normalize user-supplied DOIs for filtering (lowercase, strip prefixes)."""
    if not exclude_dois:
        return set()
    return {zotero._normalize_doi(d) for d in exclude_dois if d}


def _filter_excluded_works(
    works: list[dict], exclude_norm: set[str],
) -> tuple[list[dict], int]:
    """Drop works whose DOI (normalized) is in exclude_norm. Returns (kept, dropped_count)."""
    if not exclude_norm:
        return works, 0
    kept: list[dict] = []
    dropped = 0
    for w in works:
        w_doi = (w.get("doi") or "").replace("https://doi.org/", "")
        if w_doi and zotero._normalize_doi(w_doi) in exclude_norm:
            dropped += 1
            continue
        kept.append(w)
    return kept, dropped


def _format_citation_results(
    results: list[dict],
    doi: str,
    direction: str,
    total_count: int,
    zot_index: set | None = None,
) -> str:
    """Format OpenAlex citation/reference results for LLM consumption."""
    if not results:
        return (
            f"No {direction} found for DOI: {doi}\n"
            f"(Total count from OpenAlex: {total_count})"
        )

    lines = [
        f"{direction.title()} for DOI: {doi}",
        f"Showing {len(results)} of {total_count:,} total",
        "=" * 60,
        "",
    ]

    for i, work in enumerate(results):
        work_doi = (work.get("doi") or "").replace("https://doi.org/", "")

        # Authors
        authorships = work.get("authorships") or []
        author_names = [
            a.get("author", {}).get("display_name", "")
            for a in authorships[:4]
        ]
        authors_str = ", ".join(n for n in author_names if n)
        if len(authorships) > 4:
            authors_str += f" +{len(authorships) - 4} more"

        # Venue
        primary_loc = work.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        venue = source.get("display_name") or ""

        # OpenAlex Work ID
        openalex_id = (work.get("id") or "").split("/")[-1]

        cited_by = work.get("cited_by_count", 0)

        # Zotero membership
        doi_norm = zotero._normalize_doi(work_doi) if (work_doi and zot_index is not None) else None
        in_zot = doi_norm in zot_index if (doi_norm and zot_index is not None) else False

        lines.append(f"[{i}] {work.get('title', 'Untitled')}")
        if in_zot:
            lines.append("    ★ IN ZOTERO")
        if authors_str:
            lines.append(f"    Authors: {authors_str}")
        lines.append(f"    Year: {work.get('publication_year', '?')}")
        if venue:
            lines.append(f"    Venue: {venue}")
        lines.append(f"    Citations: {cited_by:,}")
        if work_doi:
            lines.append(f"    DOI: {work_doi}")
        if openalex_id:
            lines.append(f"    OpenAlex: {openalex_id}")

        # Reconstruct abstract from inverted index
        abstract_inv = work.get("abstract_inverted_index")
        if abstract_inv:
            abstract = _reconstruct_abstract(abstract_inv)
            if abstract:
                if len(abstract) > 300:
                    abstract = abstract[:300] + "..."
                lines.append(f"    Abstract: {abstract}")

        if work_doi:
            lines.append(f"    → fetch_fulltext(doi=\"{work_doi}\") to read")

        lines.append("")

    # Footer with workflow hints
    dois_available = [
        (w.get("doi") or "").replace("https://doi.org/", "")
        for w in results if w.get("doi")
    ]
    if dois_available:
        sample = dois_available[:5]
        lines.append("─" * 60)
        lines.append("Next steps:")
        lines.append(f"→ batch_sections(dois={json.dumps(sample)}) to survey these papers")
        lines.append("→ get_citations / get_references on any result to continue exploring the graph")
        if direction == "citations":
            lines.append(f"→ get_references(doi=\"{doi}\") to also see what this paper cites")
        else:
            lines.append(f"→ get_citations(doi=\"{doi}\") to also see what cites this paper")

    return "\n".join(lines)


async def _handle_get_citations(args: dict) -> list[TextContent]:
    """Find forward citations (papers that cite the given DOI)."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 25), 50)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")
    exclude_norm = _normalize_exclude_dois(args.get("exclude_dois"))
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    try:
        data, zot_index = await asyncio.gather(
            apis.openalex_citations(
                doi, search=keywords, limit=fetch_limit,
                start_year=start_year, end_year=end_year,
                openalex_id=openalex_id,
            ),
            zotero.get_doi_index(),
            return_exceptions=True,
        )
    except Exception as e:
        return [TextContent(type="text", text=f"Error fetching citations for {doi}: {e}")]

    if isinstance(data, Exception):
        return [TextContent(type="text", text=f"Error fetching citations for {doi}: {data}")]
    if isinstance(zot_index, Exception):
        zot_index = set()

    results = data.get("results", [])
    results, dropped = _filter_excluded_works(results, exclude_norm)
    results = results[:limit]
    total = data.get("meta", {}).get("count", len(results))
    text = _format_citation_results(results, doi, "citations", total, zot_index)
    if dropped:
        text += f"\n\n(Filtered out {dropped} result(s) matching exclude_dois.)"
    return [TextContent(type="text", text=text)]


async def _handle_get_references(args: dict) -> list[TextContent]:
    """Find backward references (papers cited by the given DOI)."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 25), 50)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")
    exclude_norm = _normalize_exclude_dois(args.get("exclude_dois"))
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    try:
        data, zot_index = await asyncio.gather(
            apis.openalex_references(
                doi, search=keywords, limit=fetch_limit,
                start_year=start_year, end_year=end_year,
                openalex_id=openalex_id,
            ),
            zotero.get_doi_index(),
            return_exceptions=True,
        )
    except Exception as e:
        return [TextContent(type="text", text=f"Error fetching references for {doi}: {e}")]

    if isinstance(data, Exception):
        return [TextContent(type="text", text=f"Error fetching references for {doi}: {data}")]
    if isinstance(zot_index, Exception):
        zot_index = set()

    results = data.get("results", [])
    results, dropped = _filter_excluded_works(results, exclude_norm)
    results = results[:limit]
    total = data.get("meta", {}).get("count", len(results))
    text = _format_citation_results(results, doi, "references", total, zot_index)
    if dropped:
        text += f"\n\n(Filtered out {dropped} result(s) matching exclude_dois.)"
    return [TextContent(type="text", text=text)]


async def _handle_get_citation_tree(args: dict) -> list[TextContent]:
    """Get both citations and references concurrently."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 10), 25)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")

    # Resolve the OpenAlex ID ONCE before forking into concurrent tasks so
    # neither branch independently calls openalex_work(doi).
    if not openalex_id:
        try:
            resolved_id = await apis._resolve_openalex_filter_id(doi)
        except Exception:
            resolved_id = None
    else:
        resolved_id = openalex_id

    exclude_norm = _normalize_exclude_dois(args.get("exclude_dois"))
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    cit_data, ref_data, zot_index = await asyncio.gather(
        apis.openalex_citations(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=resolved_id,
        ),
        apis.openalex_references(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=resolved_id,
        ),
        zotero.get_doi_index(),
        return_exceptions=True,
    )

    if isinstance(zot_index, Exception):
        zot_index = set()

    parts = []

    if isinstance(cit_data, Exception):
        parts.append(f"⚠ Citations lookup failed: {cit_data}")
    else:
        results = cit_data.get("results", [])
        results, cit_dropped = _filter_excluded_works(results, exclude_norm)
        results = results[:limit]
        total = cit_data.get("meta", {}).get("count", len(results))
        parts.append(_format_citation_results(results, doi, "citations", total, zot_index))
        if cit_dropped:
            parts.append(f"(Filtered out {cit_dropped} citation(s) matching exclude_dois.)")

    parts.append("\n" + "═" * 60 + "\n")

    if isinstance(ref_data, Exception):
        parts.append(f"⚠ References lookup failed: {ref_data}")
    else:
        results = ref_data.get("results", [])
        results, ref_dropped = _filter_excluded_works(results, exclude_norm)
        results = results[:limit]
        total = ref_data.get("meta", {}).get("count", len(results))
        parts.append(_format_citation_results(results, doi, "references", total, zot_index))
        if ref_dropped:
            parts.append(f"(Filtered out {ref_dropped} reference(s) matching exclude_dois.)")

    return [TextContent(type="text", text="\n".join(parts))]


_BOOK_TYPES = {"book", "edited-book", "monograph", "reference-book"}
_CHAPTER_TYPES = {"book-chapter", "reference-entry", "book-part", "book-section"}


def _page_sort_key(item: dict) -> tuple:
    """Sort chapters by first page number when available; otherwise by title."""
    page = item.get("page") or ""
    first = page.split("-")[0].strip()
    try:
        return (0, int(first))
    except (TypeError, ValueError):
        return (1, (item.get("title") or [""])[0].lower())


async def _handle_get_book_chapters(args: dict) -> list[TextContent]:
    """List chapters sharing an ISBN or book title — drill up/down between book and chapters."""
    raw_doi = (args.get("doi") or "").strip()
    raw_isbn = (args.get("isbn") or "").strip()
    keywords = args.get("keywords")
    limit = min(max(args.get("limit", 50), 1), 100)

    if not raw_doi and not raw_isbn:
        return [TextContent(type="text", text="Provide either 'doi' or 'isbn'.")]

    # Resolve seed metadata
    seed: dict | None = None
    seed_doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
    isbns: list[str] = []
    container_title: str | None = None
    seed_type: str = ""

    if seed_doi:
        try:
            seed = await apis.crossref_work(seed_doi)
        except Exception as e:
            logger.debug("Crossref lookup failed for %s: %s", seed_doi, e)
        if seed:
            seed_type = (seed.get("type") or "").lower()
            isbns = [apis._normalize_isbn(i) for i in (seed.get("ISBN") or []) if i]
            # For a chapter, container-title is the book title; for a book itself it's empty.
            ct_list = seed.get("container-title") or []
            if ct_list:
                container_title = ct_list[0]
            elif seed_type in _BOOK_TYPES:
                title_list = seed.get("title") or []
                container_title = title_list[0] if title_list else None

    if raw_isbn:
        isbns.insert(0, apis._normalize_isbn(raw_isbn))
    # Deduplicate while preserving order
    isbns = list(dict.fromkeys([i for i in isbns if i]))

    if not isbns and not container_title:
        return [TextContent(type="text", text=(
            f"Could not resolve a book identifier from {raw_doi or raw_isbn}. "
            "Crossref returned no ISBN or container-title. Try passing the ISBN directly "
            "via the 'isbn' parameter."
        ))]

    # Query Crossref — ISBNs first (concat results), then fall back to title if empty
    items: list[dict] = []
    seen: set[str] = set()
    for isbn in isbns:
        try:
            batch = await apis.crossref_book_chapters(
                isbn=isbn, keywords=keywords, limit=limit,
            )
        except Exception as e:
            logger.warning("Crossref chapter query failed for ISBN %s: %s", isbn, e)
            continue
        for it in batch:
            d = (it.get("DOI") or "").lower()
            if d and d not in seen:
                seen.add(d)
                items.append(it)

    if not items and container_title:
        try:
            batch = await apis.crossref_book_chapters(
                container_title=container_title, keywords=keywords, limit=limit,
            )
        except Exception as e:
            logger.warning("Crossref chapter query failed for title %r: %s", container_title, e)
            batch = []
        for it in batch:
            d = (it.get("DOI") or "").lower()
            if d and d not in seen:
                seen.add(d)
                items.append(it)

    items.sort(key=_page_sort_key)
    items = items[:limit]

    # Header — describe what we resolved
    book_title = container_title or ""
    if seed and seed_type in _BOOK_TYPES and not book_title:
        title_list = seed.get("title") or []
        book_title = title_list[0] if title_list else ""

    lines = []
    if seed_type in _CHAPTER_TYPES and seed_doi:
        lines.append(f"Drilled UP from chapter {seed_doi} → sibling chapters in:")
    elif seed_type in _BOOK_TYPES and seed_doi:
        lines.append(f"Drilled DOWN from book {seed_doi} → chapters:")
    else:
        lines.append("Chapters matching the provided identifier:")
    if book_title:
        lines.append(f"  Book: {book_title}")
    if isbns:
        lines.append(f"  ISBN: {', '.join(isbns)}")
    lines.append("=" * 60)
    lines.append("")

    if not items:
        lines.append(
            "No chapters found in Crossref for this volume.\n\n"
            "Likely causes: the book is not registered at the chapter level "
            "(common for older/trade books), or Crossref does not have complete "
            "coverage for this publisher. You can still fetch the main work via "
            "fetch_fulltext if a full-book PDF exists."
        )
        return [TextContent(type="text", text="\n".join(lines))]

    lines.append(f"Found {len(items)} chapter(s):\n")

    zot_index = await zotero.get_doi_index()
    current_doi_norm = zotero._normalize_doi(seed_doi) if seed_doi else None

    for i, it in enumerate(items):
        ch_doi = (it.get("DOI") or "").strip()
        ch_doi_norm = zotero._normalize_doi(ch_doi) if ch_doi else None
        title = (it.get("title") or [""])[0]
        authors = []
        for a in (it.get("author") or [])[:4]:
            name = (a.get("given", "") + " " + a.get("family", "")).strip()
            if name:
                authors.append(name)
        page = it.get("page") or ""
        marker = "  ← THIS ONE" if ch_doi_norm and ch_doi_norm == current_doi_norm else ""
        in_zot = ch_doi_norm in zot_index if ch_doi_norm else False
        zot_badge = "  ★ IN ZOTERO" if in_zot else ""

        lines.append(f"[{i}] {title}{marker}{zot_badge}")
        if authors:
            more = f" +{len(it.get('author') or []) - 4} more" if len(it.get("author") or []) > 4 else ""
            lines.append(f"    Authors: {', '.join(authors)}{more}")
        if page:
            lines.append(f"    Pages: {page}")
        if ch_doi:
            lines.append(f"    DOI: {ch_doi}")
            lines.append(f"    → fetch_fulltext(doi=\"{ch_doi}\", mode=\"sections\") to read")
        lines.append("")

    # Footer: workflow hints
    dois = [it.get("DOI") for it in items if it.get("DOI")]
    if dois:
        lines.append("─" * 60)
        lines.append("Next steps:")
        lines.append(
            "→ batch_sections(dois=[...]) to survey several chapters in parallel"
        )
        if keywords is None:
            lines.append(
                "→ Re-call with keywords='...' to narrow to chapters on a specific subtopic"
            )

    return [TextContent(type="text", text="\n".join(lines))]


async def _extract_from_landing_page(
    url: str,
    use_proxy: bool,
    expected_doi: str | None = None,
) -> dict | None:
    """Fetch a landing-page URL via stealth browser and extract content.

    Shared by both the DOI landing-page tier (pass *expected_doi* for the
    publisher-redirect mismatch guard) and the URL-driven tier (no expected_doi).

    Returns a dict with keys ``text``, ``source``, ``pdf_path``,
    ``sections``, ``section_detection``, ``word_count`` on success,
    or ``None`` on failure.  Exactly one of *text* or *pdf_path* is non-None.
    """
    if not config.use_stealth_browser:
        return None

    scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(url)
    effective_url = final_url or url

    if scrapling_path:
        return {
            "text": None, "source": "scrapling_direct_pdf",
            "pdf_path": scrapling_path,
            "sections": None, "section_detection": None, "word_count": None,
        }

    if not html:
        return None

    meta = content_extractor.extract_citation_meta(html, effective_url)

    # DOI mismatch guard: some publisher resolvers redirect to a different
    # article on lookup failure.  Only applied when caller supplies expected_doi.
    if expected_doi:
        citation_doi = meta.get("citation_doi", "")
        if citation_doi and zotero._normalize_doi(citation_doi) != zotero._normalize_doi(expected_doi):
            logger.warning(
                "DOI mismatch: requested %s, page reports %s — discarding HTML",
                expected_doi, citation_doi,
            )
            return None

    citation_pdf = meta.get("citation_pdf_url", "")
    if citation_pdf:
        logger.info("Found citation_pdf_url: %s", citation_pdf)
        path = await pdf_fetcher.fetch_direct(citation_pdf)
        if not path and use_proxy:
            path = await pdf_fetcher.fetch_proxied(citation_pdf)
        if path:
            return {
                "text": None, "source": "citation_pdf_url (direct)",
                "pdf_path": path,
                "sections": None, "section_detection": None, "word_count": None,
            }

    # Trafilatura HTML extraction.
    extraction = await content_extractor.extract_article_with_sections(html, effective_url)
    if extraction:
        raw_text = extraction["text"]
        sections = extraction["sections"] or content_extractor.detect_sections_from_text(raw_text)
        section_det = extraction["section_detection"] if extraction["sections"] else "text_heuristic"
        return {
            "text": raw_text,
            "source": f"html_extraction ({extraction['source']})",
            "pdf_path": None,
            "sections": sections,
            "section_detection": section_det,
            "word_count": extraction["word_count"],
        }

    # Last-resort PDF link scan from HTML.
    pdf_link = pdf_fetcher._extract_pdf_link_from_html(html, effective_url)
    if pdf_link:
        logger.info("Trying PDF link found in HTML: %s", pdf_link)
        path = await pdf_fetcher.fetch_direct(pdf_link)
        if not path and use_proxy:
            path = await pdf_fetcher.fetch_proxied(pdf_link)
        if path:
            return {
                "text": None, "source": "html_pdf_link",
                "pdf_path": path,
                "sections": None, "section_detection": None, "word_count": None,
            }

    return None


async def _handle_fetch_pdf(args: dict) -> list[TextContent]:
    zotero_key = (args.get("zotero_key") or "").strip() or None
    doi = args.get("doi")
    url = (args.get("url") or "").strip() or None
    if not doi and not zotero_key and not url:
        return [TextContent(
            type="text",
            text="fetch_fulltext requires at least one of 'doi', 'zotero_key', or 'url'.",
        )]
    if zotero_key and not doi:
        # Synthesize a stable cache key so the article cache works uniformly.
        doi = f"zotero:{zotero_key}"
    if not doi and url:
        # URL-only: synthesize a stable cache key from the URL hash.
        import hashlib as _hashlib
        _url_hash = _hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        doi = f"url:{_url_hash}"
    use_proxy = args.get("use_proxy", False)
    pages_str = args.get("pages")
    mode = args.get("mode", "sections")
    section_name = args.get("section")
    range_start = args.get("range_start")
    range_end = args.get("range_end")
    force_html = args.get("source", "auto") == "html"

    # Initialize variables for failure message
    html = None
    extraction = None

    # ── Cache read: fastest path — skip all network/PDF work ────────
    # Bypassed when source='html' so the caller can force a fresh scrape.
    cached_article = text_cache.get_cached(doi) if not force_html else None
    if cached_article:
        logger.debug("Article cache hit for %s", doi)
        # Re-run text heuristic when sections are empty OR when the cached
        # article used the text_heuristic path (may have been populated by an
        # older version of the heuristic that lacked footnote/running-header
        # filtering).  HTML and PDF font-analysis entries are high-confidence
        # and not re-processed.
        _should_redetect = (
            mode in ("sections", "section", "preview", "range")
            and cached_article.section_detection in ("text_heuristic", "unknown", "")
        )
        if _should_redetect:
            new_sections = content_extractor.detect_sections_from_text(
                cached_article.text,
                is_ocr=cached_article.metadata.get("is_ocr", False),
            )
            if new_sections != cached_article.sections:
                logger.debug(
                    "Re-ran section detection for %s: %d → %d sections",
                    doi, len(cached_article.sections), len(new_sections),
                )
                cached_article = text_cache.put_cached(
                    cached_article.doi,
                    cached_article.text,
                    cached_article.source,
                    sections=new_sections,
                    section_detection="text_heuristic",
                    word_count=cached_article.word_count,
                    metadata=cached_article.metadata,
                )

        # Lazy upgrade: re-extract with pymupdf4llm when the cached entry used
        # the old font-analysis pipeline and the original PDF is still on disk.
        _should_reextract = (
            config.use_pymupdf4llm
            and cached_article.section_detection in ("pdf_font_analysis", "pdf_toc")
        )
        if _should_reextract and mode in ("sections", "section", "preview", "range"):
            import hashlib
            doi_hash = hashlib.md5(doi.encode()).hexdigest()
            pdf_path = config.pdf_cache_dir / f"{doi_hash}.pdf"
            if pdf_path.exists():
                try:
                    new_result = pdf_extractor.extract_text_pymupdf4llm(pdf_path)
                    cached_article = text_cache.put_cached(
                        cached_article.doi,
                        new_result["text"],
                        cached_article.source,
                        sections=new_result["sections"],
                        section_detection=new_result.get("section_detection", "pymupdf4llm_markdown"),
                        word_count=len(new_result["text"].split()),
                        metadata=cached_article.metadata,
                    )
                    logger.info(
                        "Re-extracted %s with pymupdf4llm: %d → %d sections",
                        doi, len(cached_article.sections or []), len(new_result["sections"]),
                    )
                except Exception as exc:
                    logger.warning("pymupdf4llm re-extraction failed for %s: %s", doi, exc)

        return _apply_mode_filter(
            cached_article, mode, section_name, range_start, range_end
        )

    # Reject force_html early if the stealth browser is not available
    if force_html and not config.use_stealth_browser:
        return [TextContent(
            type="text",
            text=(
                "source='html' requires the stealth browser (USE_STEALTH_BROWSER=true). "
                "The stealth browser is not currently enabled in config."
            ),
        )]

    # Acquire per-DOI lock before fetching to prevent duplicate work
    lock = await _get_doi_lock(doi)
    result: list[TextContent] | None = None
    try:
        async with lock:
            # Double-check cache after acquiring lock — another concurrent call
            # may have fetched and cached this DOI while we were waiting.
            # Skip this for force_html — the caller explicitly wants a fresh scrape.
            cached_article = text_cache.get_cached(doi) if not force_html else None
            if cached_article:
                logger.debug("Article cache hit after lock for %s", doi)
                result = _apply_mode_filter(
                    cached_article, mode, section_name, range_start, range_end
                )
            else:
                # ── Step 0: Check Zotero FIRST ──────────────────────────────────
                if zotero_key:
                    zot_result = await zotero.get_paper_from_zotero_by_key(zotero_key)
                else:
                    zot_result = await zotero.get_paper_from_zotero(doi)
                if zot_result and zot_result.get("found"):
                    # Got fulltext directly (already extracted by Zotero — best case!)
                    if zot_result.get("text"):
                        raw_text = zot_result["text"]
                        sections = content_extractor.detect_sections_from_text(raw_text)
                        # Extract metadata from Zotero item if available
                        zot_meta: dict = {}
                        if zot_result.get("item"):
                            item = zot_result["item"]
                            zot_meta["title"] = item.get("title", "")
                            zot_meta["year"] = str(item.get("date", ""))[:4] if item.get("date") else ""
                            zot_meta["venue"] = item.get("publicationTitle", "")
                            creators = item.get("creators", [])
                            if creators:
                                zot_meta["authors"] = [c.get("lastName", "") or c.get("firstName", "") for c in creators]
                        cached_article = text_cache.put_cached(
                            doi, raw_text, zot_result["source"],
                            sections=sections,
                            section_detection="text_heuristic",
                            word_count=len(raw_text.split()),
                            metadata=zot_meta,
                        )
                        if mode != "full":
                            result = _apply_mode_filter(
                                cached_article, mode, section_name, range_start, range_end
                            )
                        else:
                            header = f"Full text from Zotero (already indexed) for DOI: {doi}\n"
                            header += f"Source: {zot_result['source']}\n"
                            if zot_result.get("indexed_pages") and zot_result.get("total_pages"):
                                header += f"Pages indexed: {zot_result['indexed_pages']}/{zot_result['total_pages']}\n"
                            if zot_result.get("truncated"):
                                header += (
                                    "⚠ WARNING: Text is TRUNCATED — Zotero only indexed "
                                    f"{zot_result.get('indexed_pages', '?')}/{zot_result.get('total_pages', '?')} pages. "
                                    "Increase in Zotero > Settings > Search > PDF Indexing, reindex, and sync.\n"
                                    "Alternatively, use fetch_fulltext with use_proxy=true to get the full PDF.\n"
                                )
                            header += "=" * 60 + "\n\n"
                            text = header + raw_text
                            if len(text) > config.max_context_length:
                                text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
                            result = [TextContent(type="text", text=text)]

                        # Add citation header for full mode
                        if result and mode == "full":
                            citation_header = _format_citation_header(doi, zot_meta)
                            result = [TextContent(type="text", text=citation_header + result[0].text)]

                    # Got PDF path from Zotero — extract text from disk
                    elif zot_result.get("pdf_path"):
                        result = _cache_pdf_and_return(
                            zot_result["pdf_path"], doi, zot_result["source"],
                            pages_str, mode, section_name, range_start, range_end,
                        )

                # For Zotero-only (no real DOI), the rest of the pipeline has
                # nothing to work with unless the Zotero item has a `url` field.
                # Check for that first; if found, promote it so the URL tier
                # (below) can handle it.
                if result is None and zotero_key:
                    if url is None:
                        # Try to read the `url` field from the Zotero item.
                        try:
                            _zot_item = await zotero_sqlite.search_by_key(zotero_key)
                            if _zot_item and (_zot_item.url or "").strip():
                                url = _zot_item.url.strip()
                                import hashlib as _hashlib2
                                _url_hash2 = _hashlib2.sha256(url.encode("utf-8")).hexdigest()[:16]
                                doi = f"url:{_url_hash2}"
                                logger.info(
                                    "zotero_key %s has no attachment but has url=%s; "
                                    "delegating to URL tier.",
                                    zotero_key, url,
                                )
                        except Exception as _e:
                            logger.debug("Failed to look up Zotero url for %s: %s", zotero_key, _e)

                    if result is None and url is None:
                        zot_source = (zot_result or {}).get("source") or "not found"
                        meta = (zot_result or {}).get("metadata") or {}
                        title = meta.get("title") or "(unknown title)"
                        return [TextContent(
                            type="text",
                            text=(
                                f"Zotero item {zotero_key} ({title}) has no indexed fulltext "
                                f"or retrievable PDF (source: {zot_source}). No DOI is available "
                                "to try open-access or proxy fallbacks. "
                                "In Zotero, check that the item has a PDF attachment and that "
                                "PDF indexing has run (Settings > Search)."
                            ),
                        )]

                if result is None:
                    # ── Tier 0.5: URL-driven fetch ──────────────────────────────────
                    # Fires when a URL is available (passed in directly, or promoted
                    # from the Zotero item's url field above).  Runs before the
                    # DOI-based tiers; those tiers are no-ops for synthetic url:* keys.
                    if url:
                        logger.info("Trying URL tier for %s", url)
                        from urllib.parse import urlparse as _urlparse
                        _parsed = _urlparse(url)
                        _url_path_lower = (_parsed.path or "").lower()
                        _url_pdf_path: "Path | None" = None

                        # Fast path: URL ends in .pdf — try direct download first.
                        if _url_path_lower.endswith(".pdf"):
                            try:
                                _url_pdf_path = await pdf_fetcher.fetch_direct(url)
                            except Exception as _ue:
                                logger.info("URL tier: direct PDF fetch failed for %s: %s", url, _ue)

                        # Content-Type probe: some URLs serve PDFs without .pdf extension.
                        if not _url_pdf_path and not _url_path_lower.endswith(".pdf"):
                            try:
                                async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as _hc:
                                    _head = await _hc.head(url)
                                    _ctype = (_head.headers.get("content-type") or "").lower()
                                    if "application/pdf" in _ctype:
                                        _url_pdf_path = await pdf_fetcher.fetch_direct(url)
                            except Exception as _ue2:
                                logger.debug("URL tier: HEAD probe failed for %s: %s", url, _ue2)

                        if _url_pdf_path:
                            result = _cache_pdf_and_return(
                                _url_pdf_path, doi, "url_direct_pdf",
                                pages_str, mode, section_name, range_start, range_end,
                            )

                        # Landing-page path: stealth browser + trafilatura pipeline.
                        if result is None and config.use_stealth_browser:
                            try:
                                _lp = await _extract_from_landing_page(url, use_proxy)
                                if _lp:
                                    if _lp["pdf_path"]:
                                        result = _cache_pdf_and_return(
                                            _lp["pdf_path"], doi, _lp["source"],
                                            pages_str, mode, section_name, range_start, range_end,
                                        )
                                    elif _lp["text"]:
                                        _raw = _lp["text"]
                                        _cached_lp = text_cache.put_cached(
                                            doi, _raw, _lp["source"],
                                            sections=_lp["sections"],
                                            section_detection=_lp["section_detection"],
                                            word_count=_lp["word_count"],
                                            metadata={"url": url},
                                        )
                                        if mode != "full":
                                            result = _apply_mode_filter(
                                                _cached_lp, mode, section_name, range_start, range_end
                                            )
                                        else:
                                            _text = (
                                                f"Full text extracted from URL: {url}\n"
                                                f"Source: {_lp['source']}\n"
                                                f"Word count: {_lp['word_count']}\n"
                                                f"{'=' * 60}\n\n" + _raw
                                            )
                                            if len(_text) > config.max_context_length:
                                                _text = (
                                                    _text[:config.max_context_length]
                                                    + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                                )
                                            result = [TextContent(type="text", text=_text)]
                            except Exception as _ue3:
                                logger.info("URL tier: landing-page extraction failed for %s: %s", url, _ue3)

                        # If URL tier succeeded and doi is synthetic, return now.
                        # If URL tier failed and doi is synthetic (url:*), bail out early
                        # — there are no DOI-based tiers that can help.
                        if doi.startswith("url:") and result is None:
                            return [TextContent(
                                type="text",
                                text=(
                                    f"Could not retrieve content from URL: {url}\n\n"
                                    "Tried: direct PDF download"
                                    + (", stealth browser + HTML extraction" if config.use_stealth_browser else "")
                                    + ".\n"
                                    "Check that the URL is publicly accessible, or save the "
                                    "document to Zotero and retry with zotero_key."
                                ),
                            )]

                    # ── Step 0b: SSRN DOI remapping ─────────────────────────────────
                    # For SSRN preprints, try to find the published version's DOI and
                    # any OA PDF URLs before doing any network fetching.  We skip this
                    # when re-entering the pipeline via a remap (to avoid recursion).
                    _ssrn_remap: dict | None = None
                    if doi.startswith("10.2139/ssrn.") and not args.get("_original_ssrn_doi"):
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _c:
                            _ssrn_remap = await apis.resolve_ssrn_doi(doi, _c)

                        # Try OA PDF URLs discovered by remap
                        for _oa_url in (_ssrn_remap or {}).get("oa_pdf_urls", []):
                            _oa_path = await pdf_fetcher.fetch_direct(_oa_url)
                            if _oa_path:
                                result = _cache_pdf_and_return(
                                    _oa_path, doi, "ssrn_remap_oa",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break

                        # If a published DOI was found, re-enter pipeline with it
                        if result is None and (_ssrn_remap or {}).get("published_doi"):
                            _pub_doi = _ssrn_remap["published_doi"]
                            logger.info("SSRN %s → published %s", doi, _pub_doi)
                            result = await _handle_fetch_pdf({
                                **args,
                                "doi": _pub_doi,
                                "_original_ssrn_doi": doi,
                            })

                        # If still nothing, try title-based search for a published version
                        if result is None and (_ssrn_remap or {}).get("title"):
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _c:
                                _title_remap = await apis.search_by_title_for_published_version(
                                    _ssrn_remap["title"], doi, _c
                                )
                            if _title_remap and _title_remap.get("published_doi"):
                                logger.info(
                                    "SSRN title search %r → %s",
                                    _ssrn_remap["title"][:60], _title_remap["published_doi"],
                                )
                                result = await _handle_fetch_pdf({
                                    **args,
                                    "doi": _title_remap["published_doi"],
                                    "_original_ssrn_doi": doi,
                                })

                    # ── Step 1: Gather candidate PDF URLs from external APIs ────────
                    s2_paper = None
                    oa_paper = None
                    unpaywall_data = None

                    try:
                        s2_paper = await apis.s2_paper(f"DOI:{doi}")
                    except Exception:
                        pass
                    try:
                        oa_paper = await apis.openalex_work(doi)
                    except Exception:
                        pass
                    try:
                        unpaywall_data = await apis.unpaywall_lookup(doi)
                    except Exception:
                        pass

                    # Build citation metadata from whatever API data we have
                    cite_meta: dict = {}
                    if s2_paper:
                        cite_meta["title"] = s2_paper.get("title", "")
                        cite_meta["year"] = s2_paper.get("year", "")
                        cite_meta["venue"] = s2_paper.get("venue", "")
                        authors = s2_paper.get("authors", [])
                        if authors:
                            cite_meta["authors"] = [a.get("name", "") for a in authors if a.get("name")]
                    if oa_paper and not cite_meta.get("title"):
                        cite_meta["title"] = oa_paper.get("title", "")
                        cite_meta["year"] = str(oa_paper.get("publication_year", ""))
                        # OpenAlex has authorship structure
                        authorships = oa_paper.get("authorships", [])
                        if authorships:
                            cite_meta["authors"] = [
                                a.get("author", {}).get("display_name", "")
                                for a in authorships if a.get("author", {}).get("display_name")
                            ]

                    candidate_urls = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)

                    # ── Step 2: Direct HTTP on candidate URLs (fast; handles arXiv/OA) ──
                    config.evict_cache_lru()
                    for candidate in candidate_urls:
                        url = candidate["url"]
                        source = candidate["source"]
                        pdf_cached = pdf_fetcher._cache_path(url)
                        if pdf_cached.exists() and pdf_fetcher._is_pdf_file(pdf_cached):
                            logger.info("Cache hit for %s", url)
                            result = _cache_pdf_and_return(
                                pdf_cached, doi, f"{source} (cached)",
                                pages_str, mode, section_name, range_start, range_end,
                            )
                            if result:
                                break
                        path = await pdf_fetcher.fetch_direct(url)
                        if path:
                            result = _cache_pdf_and_return(
                                path, doi, f"{source} (direct)",
                                pages_str, mode, section_name, range_start, range_end,
                            )
                            if result:
                                break

                    # ── Step 2b: CORE.ac.uk ─────────────────────────────────────────
                    if result is None and config.core_api_key:
                        _core_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _cc:
                            # DOI lookup first, then title if no direct hit
                            _core_hits = await core_api.search_core(doi=doi, client=_cc)
                            if not _core_hits and _core_title:
                                _core_hits = await core_api.search_core(title=_core_title, client=_cc)
                            for _ch in _core_hits:
                                if _ch.get("core_id"):
                                    _core_path = await core_api.download_from_core(
                                        _ch["core_id"], _ch.get("download_url"), _cc
                                    )
                                    if _core_path:
                                        result = _cache_pdf_and_return(
                                            _core_path, doi, "core.ac.uk",
                                            pages_str, mode, section_name, range_start, range_end,
                                        )
                                        if result:
                                            break
                                # Also try sourceFulltextUrls directly
                                if result is None:
                                    for _src_url in _ch.get("source_fulltext_urls") or []:
                                        _src_path = await pdf_fetcher.fetch_direct(_src_url)
                                        if _src_path:
                                            result = _cache_pdf_and_return(
                                                _src_path, doi, "core_source_url",
                                                pages_str, mode, section_name, range_start, range_end,
                                            )
                                            if result:
                                                break
                                if result:
                                    break

                    # ── Step 2c: Web search fallback (Serper / Brave) ────────────────
                    if result is None and (config.serper_api_key or config.brave_search_api_key):
                        _ws_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        if _ws_title:
                            _ws_authors = cite_meta.get("authors") or []
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _wc:
                                _ws_hits = await web_search.search_for_pdf(
                                    _ws_title, _ws_authors, _wc
                                )
                            for _wh in _ws_hits:
                                _ws_url = _wh["url"]
                                # Only attempt direct PDF URLs; landing pages are
                                # handled by the stealth browser tier (step 3).
                                _ws_url_lower = _ws_url.lower()
                                if (
                                    not _ws_url_lower.endswith(".pdf")
                                    and "viewcontent.cgi" not in _ws_url_lower
                                ):
                                    continue
                                _ws_path = await pdf_fetcher.fetch_direct(_ws_url)
                                if not _ws_path and use_proxy:
                                    _ws_path = await pdf_fetcher.fetch_proxied(_ws_url)
                                if _ws_path:
                                    # Validate: is this actually the paper we wanted?
                                    if not pdf_fetcher._pdf_matches_expected_paper(
                                        _ws_path, _ws_title, _ws_authors
                                    ):
                                        logger.info(
                                            "Web search result rejected by validation: %s",
                                            _ws_url,
                                        )
                                        try:
                                            _ws_path.unlink(missing_ok=True)
                                        except OSError:
                                            pass
                                        continue
                                    result = _cache_pdf_and_return(
                                        _ws_path, doi, f"web_search ({_wh['source']})",
                                        pages_str, mode, section_name, range_start, range_end,
                                    )
                                    if result:
                                        break

                    # ── Step 3: Scrapling fetch of DOI landing page ──────────────────
                    #
                    # For normal fetches: delegates to _extract_from_landing_page which
                    # handles citation_pdf_url, trafilatura, and PDF link scanning.
                    # For force_html: keeps inline for the specialised cache-refresh logic.
                    if result is None and not force_html and not doi.startswith("url:"):
                        doi_url = (
                            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                        )
                        lp = await _extract_from_landing_page(doi_url, use_proxy, expected_doi=doi)
                        if lp:
                            if lp["pdf_path"]:
                                result = _cache_pdf_and_return(
                                    lp["pdf_path"], doi, lp["source"],
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                            elif lp["text"]:
                                raw_text = lp["text"]
                                cached_article = text_cache.put_cached(
                                    doi, raw_text, lp["source"],
                                    sections=lp["sections"],
                                    section_detection=lp["section_detection"],
                                    word_count=lp["word_count"],
                                    metadata=cite_meta,
                                )
                                if mode != "full":
                                    result = _apply_mode_filter(
                                        cached_article, mode, section_name, range_start, range_end
                                    )
                                else:
                                    text = (
                                        f"Full text extracted from DOI: {doi}\n"
                                        f"Source: {lp['source']}\n"
                                        f"Word count: {lp['word_count']}\n"
                                        f"{'=' * 60}\n\n" + raw_text
                                    )
                                    if len(text) > config.max_context_length:
                                        text = (
                                            text[:config.max_context_length]
                                            + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                        )
                                    citation_header = _format_citation_header(doi, cite_meta)
                                    result = [TextContent(type="text", text=citation_header + text)]

                    if force_html:
                        doi_url = (
                            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                        )
                        scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(
                            doi_url
                        )
                        # force_html: skip direct PDF, use HTML extraction only.
                        if html:
                            effective_url = final_url or doi_url
                            meta = content_extractor.extract_citation_meta(html, effective_url)
                            citation_doi = meta.get("citation_doi", "")
                            if citation_doi and zotero._normalize_doi(citation_doi) != zotero._normalize_doi(doi):
                                logger.warning(
                                    "DOI mismatch: requested %s, page reports %s — discarding HTML",
                                    doi, citation_doi,
                                )
                                html = None
                            if html:
                                extraction = await content_extractor.extract_article_with_sections(
                                    html, effective_url
                                )
                                if extraction:
                                    raw_text = extraction["text"]
                                    sections = extraction["sections"] or content_extractor.detect_sections_from_text(raw_text)
                                    section_det = extraction["section_detection"] if extraction["sections"] else "text_heuristic"
                                    html_source = f"html_extraction ({extraction['source']})"
                                    html_words = extraction["word_count"]
                                    html_sections = len(sections)
                                    html_is_good = html_words > 1500 and html_sections >= 3
                                    if html_is_good:
                                        cached_article = text_cache.put_cached(
                                            doi, raw_text, html_source,
                                            sections=sections,
                                            section_detection=section_det,
                                            word_count=html_words,
                                            metadata=cite_meta,
                                        )
                                    else:
                                        # HTML exists but isn't good enough to replace cache
                                        cached_article = text_cache.get_cached(doi) or text_cache.put_cached(
                                            doi, raw_text, html_source,
                                            sections=sections,
                                            section_detection=section_det,
                                            word_count=html_words,
                                            metadata=cite_meta,
                                        )
                                    if mode != "full":
                                        result = _apply_mode_filter(
                                            cached_article, mode, section_name, range_start, range_end
                                        )
                                    else:
                                        text = (
                                            f"Full text extracted from DOI: {doi}\n"
                                            f"Source: {html_source}\n"
                                            f"Word count: {extraction['word_count']}\n"
                                            f"{'=' * 60}\n\n" + raw_text
                                        )
                                        if len(text) > config.max_context_length:
                                            text = (
                                                text[:config.max_context_length]
                                                + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                            )
                                        citation_header = _format_citation_header(doi, cite_meta)
                                        result = [TextContent(type="text", text=citation_header + text)]

                    # ── Step 4: Proxied fetch on candidates (institutional access) ───
                    if result is None and use_proxy and config.gost_proxy_url:
                        for candidate in candidate_urls:
                            path = await pdf_fetcher.fetch_proxied(candidate["url"])
                            if path:
                                result = _cache_pdf_and_return(
                                    path, doi, f"{candidate['source']} (proxied)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break
                        if result is None and not doi.startswith("url:"):
                            doi_url = (
                                f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                            )
                            path = await pdf_fetcher.fetch_proxied(doi_url)
                            if path:
                                result = _cache_pdf_and_return(
                                    path, doi, "doi_redirect (proxied)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )

                    # ── Step 5: Scrapling on candidate URLs (last resort) ────────────
                    if result is None and config.use_stealth_browser:
                        for candidate in candidate_urls:
                            scrap_path, _html, _scrap_url = await pdf_fetcher.fetch_with_scrapling(
                                candidate["url"]
                            )
                            if scrap_path:
                                result = _cache_pdf_and_return(
                                    scrap_path, doi, f"{candidate['source']} (scrapling)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break

                    # ── Step 6: HeinOnline (law review + institutional proxy) ────────
                    if result is None and web_search._looks_like_law_review(cite_meta):
                        _hein_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        if _hein_title:
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _hc:
                                _hein_path = await web_search.fetch_from_heinonline(_hein_title, _hc)
                            if _hein_path:
                                result = _cache_pdf_and_return(
                                    _hein_path, doi, "heinonline",
                                    pages_str, mode, section_name, range_start, range_end,
                                )

                    # ── Step 8: SSRN cookie injection ────────────────────────────────
                    if result is None and doi.startswith("10.2139/ssrn.") and config.ssrn_cookies:
                        _ssrn_id = doi.rsplit(".", 1)[-1]
                        _ssrn_page_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={_ssrn_id}"
                        logger.info("Trying SSRN cookie injection for %s", doi)
                        _ssrn_html = await web_search.fetch_ssrn_with_cookies(_ssrn_page_url)
                        if _ssrn_html:
                            # Extract direct PDF link from the SSRN page
                            _ssrn_pdf_link = pdf_fetcher._extract_pdf_link_from_html(
                                _ssrn_html, _ssrn_page_url
                            )
                            if _ssrn_pdf_link:
                                _ssrn_path = await pdf_fetcher.fetch_direct(_ssrn_pdf_link)
                                if not _ssrn_path and use_proxy:
                                    _ssrn_path = await pdf_fetcher.fetch_proxied(_ssrn_pdf_link)
                                if _ssrn_path:
                                    result = _cache_pdf_and_return(
                                        _ssrn_path, doi, "ssrn_cookies",
                                        pages_str, mode, section_name, range_start, range_end,
                                    )
                            # Also try HTML extraction if the page has full text
                            if result is None:
                                _ssrn_extraction = await content_extractor.extract_article_with_sections(
                                    _ssrn_html, _ssrn_page_url
                                )
                                if _ssrn_extraction and _ssrn_extraction.get("word_count", 0) > 1500:
                                    _raw = _ssrn_extraction["text"]
                                    _secs = _ssrn_extraction["sections"] or content_extractor.detect_sections_from_text(_raw)
                                    _cached_art = text_cache.put_cached(
                                        doi, _raw, "ssrn_cookies_html",
                                        sections=_secs,
                                        section_detection=_ssrn_extraction.get("section_detection", "text_heuristic"),
                                        word_count=_ssrn_extraction["word_count"],
                                        metadata=cite_meta,
                                    )
                                    result = _apply_mode_filter(
                                        _cached_art, mode, section_name, range_start, range_end
                                    )

                    # ── Failure ──────────────────────────────────────────────────────
                    if result is None:
                        _original_doi = args.get("_original_ssrn_doi") or doi
                        _is_ssrn = _original_doi.startswith("10.2139/ssrn.")

                        if _is_ssrn:
                            _ssrn_id = _original_doi.rsplit(".", 1)[-1]
                            _ssrn_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={_ssrn_id}"
                            _pub_note = ""
                            if (_ssrn_remap or {}).get("published_doi"):
                                _pub_note = (
                                    "\n\nNote: this paper may also be published as "
                                    f"https://doi.org/{_ssrn_remap['published_doi']} — "
                                    "try fetch_fulltext on that DOI if available.\n"
                                )
                            lines = [
                                f"Could not retrieve {_original_doi} automatically "
                                "(SSRN blocks bots).\n\n",
                                "**Surface this clickable link to the user so they can grab it themselves:**\n\n",
                                f"  → {_ssrn_url}\n\n",
                                "Recommended: open the link, then use the **Zotero browser connector** "
                                "to save the PDF + metadata to their library. Once it's in Zotero, "
                                "this MCP will find it automatically on future searches (search_papers "
                                "checks Zotero first) — no DOI juggling needed next time.\n\n",
                                "Alternatives: download the PDF and attach it directly to this "
                                "conversation, or drop it into Zotero manually.\n",
                                _pub_note,
                                "\nOnce the paper is available, re-run this request.",
                            ]
                        else:
                            sources_tried = [c["source"] for c in candidate_urls]
                            doi_url = f"https://doi.org/{doi}"
                            lines = [
                                f"Could not retrieve full text for DOI: {doi}\n",
                                f"Sources tried: {', '.join(sources_tried) or 'none found'}\n",
                            ]

                            if not use_proxy and config.gost_proxy_url:
                                lines.append(
                                    "\n→ Try with institutional proxy: "
                                    f"fetch_fulltext(doi=\"{doi}\", use_proxy=true)\n"
                                )
                            elif not config.gost_proxy_url:
                                lines.append(
                                    "\n→ No institutional proxy configured. If you have institutional access, "
                                    "configure GOST_PROXY_URL in .env.\n"
                                )

                            lines.append(f"→ Check available URLs: find_pdf_urls(doi=\"{doi}\")\n")
                            lines.append(f"→ Verify metadata: get_paper(identifier=\"{doi}\")\n")
                            lines.append(
                                f"\n**Ask the user to:**\n"
                                f"1. Open {doi_url} in their browser and download the PDF\n"
                                f"2. Save it to Zotero, or attach the PDF to this conversation\n"
                                f"\nOnce available, re-run this request."
                            )

                            if html and not extraction:
                                lines.append(
                                    "\n\nNote: The publisher page was reached but the full article text was not "
                                    "available — likely behind a paywall. Only the abstract could be accessed.\n"
                                )

                        result = [TextContent(type="text", text="".join(lines))]
    finally:
        # Cleanup: remove the lock if nobody else is waiting on it
        async with _doi_locks_lock:
            if doi in _doi_locks and not _doi_locks[doi].locked():
                del _doi_locks[doi]

    return result

    # ── Step 1: Gather candidate PDF URLs from external APIs ────────
    s2_paper = None
    oa_paper = None
    unpaywall_data = None

    try:
        s2_paper = await apis.s2_paper(f"DOI:{doi}")
    except Exception:
        pass
    try:
        oa_paper = await apis.openalex_work(doi)
    except Exception:
        pass
    try:
        unpaywall_data = await apis.unpaywall_lookup(doi)
    except Exception:
        pass

    # Build citation metadata from whatever API data we have
    cite_meta: dict = {}
    if s2_paper:
        cite_meta["title"] = s2_paper.get("title", "")
        cite_meta["year"] = s2_paper.get("year", "")
        cite_meta["venue"] = s2_paper.get("venue", "")
        authors = s2_paper.get("authors", [])
        if authors:
            cite_meta["authors"] = [a.get("name", "") for a in authors if a.get("name")]
    if oa_paper and not cite_meta.get("title"):
        cite_meta["title"] = oa_paper.get("title", "")
        cite_meta["year"] = str(oa_paper.get("publication_year", ""))
        # OpenAlex has authorship structure
        authorships = oa_paper.get("authorships", [])
        if authorships:
            cite_meta["authors"] = [
                a.get("author", {}).get("display_name", "")
                for a in authorships if a.get("author", {}).get("display_name")
            ]

    candidate_urls = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)

    # ── Step 2: Direct HTTP on candidate URLs (fast; handles arXiv/OA) ──
    config.evict_cache_lru()
    for candidate in candidate_urls:
        url = candidate["url"]
        source = candidate["source"]
        pdf_cached = pdf_fetcher._cache_path(url)
        if pdf_cached.exists() and pdf_fetcher._is_pdf_file(pdf_cached):
            logger.info("Cache hit for %s", url)
            return _cache_pdf_and_return(
                pdf_cached, doi, f"{source} (cached)",
                pages_str, mode, section_name, range_start, range_end,
            )
        path = await pdf_fetcher.fetch_direct(url)
        if path:
            return _cache_pdf_and_return(
                path, doi, f"{source} (direct)",
                pages_str, mode, section_name, range_start, range_end,
            )

    # ── Step 3: Scrapling fetch of DOI landing page ──────────────────
    #
    # Makes a single stealth-browser call to the DOI URL.  The response is
    # typically a publisher HTML page (not a PDF), so we:
    #   3a. Extract citation_pdf_url meta tag (publisher's canonical PDF URL)
    #       and try a direct/proxied HTTP fetch of it.
    #   3b. Run trafilatura on the full HTML — if the article body is present
    #       and ≥1500 words, return the extracted text without touching a PDF.
    #   3c. Store the HTML for step 4 (PDF-link regex scanning).
    stored_html: str | None = None
    stored_html_url: str | None = None
    extra_pdf_candidates: list[dict[str, str]] = []  # URLs found in the HTML

    if config.use_stealth_browser:
        doi_url = (
            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
        )
        scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(
            doi_url
        )

        if scrapling_path:
            # Rare: Scrapling received a PDF directly (no HTML page in the way)
            return _cache_pdf_and_return(
                scrapling_path, doi, "doi_redirect (scrapling)",
                pages_str, mode, section_name, range_start, range_end,
            )

        if html:
            effective_url = final_url or doi_url

            # ── 3a: citation_pdf_url meta tag ──────────────────────
            meta = content_extractor.extract_citation_meta(html, effective_url)

            # Guard: some publisher DOI resolvers redirect to a journal
            # homepage or a different article on lookup failure (Elsevier does
            # this occasionally).  If the page's embedded DOI doesn't match
            # what we requested, discard everything — HTML and PDF URL — so we
            # don't return the wrong paper's content with false confidence.
            citation_doi = meta.get("citation_doi", "")
            if citation_doi and zotero._normalize_doi(citation_doi) != zotero._normalize_doi(doi):
                logger.warning(
                    "DOI mismatch: requested %s, page reports %s — discarding HTML",
                    doi, citation_doi,
                )
                html = None

            citation_pdf = meta.get("citation_pdf_url", "") if html else ""
            if citation_pdf:
                logger.info("Found citation_pdf_url: %s", citation_pdf)
                path = await pdf_fetcher.fetch_direct(citation_pdf)
                if not path and use_proxy:
                    path = await pdf_fetcher.fetch_proxied(citation_pdf)
                if path:
                    return _cache_pdf_and_return(
                        path, doi, "citation_pdf_url (direct)",
                        pages_str, mode, section_name, range_start, range_end,
                    )
                # Fetch failed — keep as a candidate for later retry
                extra_pdf_candidates.append(
                    {"url": citation_pdf, "source": "citation_pdf_url"}
                )

            # ── 3b: trafilatura HTML extraction ────────────────────
            if html:
                extraction = await content_extractor.extract_article_with_sections(
                    html, effective_url
                )
                if extraction:
                    raw_text = extraction["text"]
                    # If no h2/h3 markers survived trafilatura, fall back to
                    # the conservative text heuristic for the section index.
                    sections = extraction["sections"] or content_extractor.detect_sections_from_text(raw_text)
                    section_det = extraction["section_detection"] if extraction["sections"] else "text_heuristic"
                    html_source = f"html_extraction ({extraction['source']})"
                    cached_article = text_cache.put_cached(
                        doi, raw_text, html_source,
                        sections=sections,
                        section_detection=section_det,
                        word_count=extraction["word_count"],
                        metadata=cite_meta,
                    )
                    if mode != "full":
                        return _apply_mode_filter(
                            cached_article, mode, section_name, range_start, range_end
                        )
                    text = (
                        f"Full text extracted from DOI: {doi}\n"
                        f"Source: {html_source}\n"
                        f"Word count: {extraction['word_count']}\n"
                        f"{'=' * 60}\n\n"
                        + raw_text
                    )
                    if len(text) > config.max_context_length:
                        text = (
                            text[: config.max_context_length]
                            + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                        )
                    return [TextContent(type="text", text=text)]

                # ── 3c: Store HTML for PDF-link scanning in step 4 ────
                stored_html = html
                stored_html_url = effective_url

    # ── Step 4: Proxied fetch on candidates (institutional access) ───
    if use_proxy and config.gost_proxy_url:
        for candidate in candidate_urls + extra_pdf_candidates:
            path = await pdf_fetcher.fetch_proxied(candidate["url"])
            if path:
                return _cache_pdf_and_return(
                    path, doi, f"{candidate['source']} (proxied)",
                    pages_str, mode, section_name, range_start, range_end,
                )
        doi_url = (
            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
        )
        path = await pdf_fetcher.fetch_proxied(doi_url)
        if path:
            return _cache_pdf_and_return(
                path, doi, "doi_redirect (proxied)",
                pages_str, mode, section_name, range_start, range_end,
            )

    # ── Step 5: PDF link extraction from stored HTML ─────────────────
    if stored_html and stored_html_url:
        pdf_link = pdf_fetcher._extract_pdf_link_from_html(
            stored_html, stored_html_url
        )
        if pdf_link:
            logger.info("Trying PDF link found in Scrapling HTML: %s", pdf_link)
            path = await pdf_fetcher.fetch_direct(pdf_link)
            if not path and use_proxy:
                path = await pdf_fetcher.fetch_proxied(pdf_link)
            if path:
                return _cache_pdf_and_return(
                    path, doi, "html_pdf_link",
                    pages_str, mode, section_name, range_start, range_end,
                )

    # ── Step 6: Scrapling on candidate URLs (last resort) ────────────
    if config.use_stealth_browser:
        all_candidates = candidate_urls + extra_pdf_candidates
        for candidate in all_candidates:
            scrap_path, _html, _url = await pdf_fetcher.fetch_with_scrapling(
                candidate["url"]
            )
            if scrap_path:
                return _cache_pdf_and_return(
                    scrap_path, doi, f"{candidate['source']} (scrapling)",
                    pages_str, mode, section_name, range_start, range_end,
                )

    # ── Failure ──────────────────────────────────────────────────────
    sources_tried = [c["source"] for c in candidate_urls]
    lines = [
        f"Could not retrieve full text for DOI: {doi}\n",
        f"Sources tried: {', '.join(sources_tried) or 'none found'}\n",
    ]

    # Suggest next actions based on what wasn't tried
    if not use_proxy and config.gost_proxy_url:
        lines.append(
            "\n→ Try with institutional proxy: "
            f"fetch_fulltext(doi=\"{doi}\", use_proxy=true)\n"
        )
    elif not config.gost_proxy_url:
        lines.append(
            "\n→ No institutional proxy configured. If you have institutional access, "
            "configure GOST_PROXY_URL in .env.\n"
        )

    lines.append(
        f"→ Check available URLs: find_pdf_urls(doi=\"{doi}\")\n"
    )
    lines.append(
        f"→ Verify metadata: get_paper(identifier=\"{doi}\")\n"
    )

    # If we got HTML but it was too short (paywall), say so explicitly
    if html and not extraction:
        lines.append(
            "\nNote: The publisher page was reached but the full article text was not "
            "available — likely behind a paywall. Only the abstract could be accessed.\n"
        )

    return [TextContent(type="text", text="".join(lines))]


async def _handle_search_and_read(args: dict) -> list[TextContent]:
    query = args["query"]
    result_index = args.get("result_index", 0)
    use_proxy = args.get("use_proxy", False)

    # Use the unified parallel pipeline so semantic Zotero, S2, OpenAlex,
    # and Primo all contribute candidates — not just S2.
    try:
        results = await _collect_search_results({
            "query": query,
            "limit": 5,
            "source": "all",
            # `semantic` honours the global default (SEMANTIC_DEFAULT_ON);
            # callers can still pass semantic=False to opt out.
            "semantic": args.get("semantic"),
        })
    except Exception as e:
        return [TextContent(type="text", text=f"Search failed: {e}")]

    if not results:
        return [TextContent(type="text", text=f"No papers found for '{query}'")]

    if result_index >= len(results):
        return [TextContent(
            type="text",
            text=f"Result index {result_index} out of range (found {len(results)} results)",
        )]

    paper = results[result_index]
    doi = paper.get("doi")
    title = paper.get("title") or "Untitled"
    sources = ", ".join(paper.get("found_in") or []) or "?"

    text = f"Selected paper [{result_index}]: {title}\n"
    text += f"Sources: {sources}\n"
    text += f"DOI: {doi}\n\n"

    # If the paper is in Zotero, fetch_fulltext via zotero_key works without a DOI.
    if not doi:
        zot_key = paper.get("zotero_key")
        if zot_key:
            fetch_result = await _handle_fetch_pdf({
                "zotero_key": zot_key,
                "use_proxy": use_proxy,
            })
            return [TextContent(type="text", text=text + fetch_result[0].text)]
        url = paper.get("url")
        if url:
            fetch_result = await _handle_fetch_pdf({
                "url": url,
                "use_proxy": use_proxy,
            })
            return [TextContent(type="text", text=text + fetch_result[0].text)]
        text += "No DOI / Zotero key / URL found for this paper. Cannot fetch full text.\n"
        if paper.get("abstract"):
            text += f"\nAbstract:\n{paper['abstract']}\n"
        return [TextContent(type="text", text=text)]

    fetch_result = await _handle_fetch_pdf({
        "doi": doi,
        "use_proxy": use_proxy,
    })

    return [TextContent(
        type="text",
        text=text + fetch_result[0].text,
    )]


async def _handle_find_pdf_urls(args: dict) -> list[TextContent]:
    doi = args["doi"]

    s2_paper = None
    oa_paper = None
    unpaywall_data = None

    try:
        s2_paper = await apis.s2_paper(f"DOI:{doi}")
    except Exception:
        pass
    try:
        oa_paper = await apis.openalex_work(doi)
    except Exception:
        pass
    try:
        unpaywall_data = await apis.unpaywall_lookup(doi)
    except Exception:
        pass

    candidate_urls = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)

    text = f"PDF URL candidates for DOI: {doi}\n\n"

    if candidate_urls:
        for i, c in enumerate(candidate_urls):
            text += f"  [{i+1}] Source: {c['source']}\n"
            text += f"      URL: {c['url']}\n\n"
    else:
        text += "No open access PDF URLs found.\n"

    # Add OA status info
    if unpaywall_data:
        text += f"\nUnpaywall OA status: {'Open Access' if unpaywall_data.get('is_oa') else 'Not OA'}\n"
        if unpaywall_data.get("oa_status"):
            text += f"OA type: {unpaywall_data['oa_status']}\n"
        if unpaywall_data.get("journal_is_oa"):
            text += "Journal is fully OA\n"

    return [TextContent(type="text", text=text)]


def _build_bm25_index(text: str, window_words: int = 300, stride_words: int = 150):
    """Build a BM25 index over overlapping word-windows of *text*.

    Returns ``(index, windows)`` where ``windows`` is a list of
    ``{"start": int, "end": int, "tokens": list[str]}`` dicts.
    """
    import re as _re
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return None, []

    # Split text into words with byte offsets
    word_spans = [(m.start(), m.end()) for m in _re.finditer(r"\S+", text)]
    if not word_spans:
        return None, []

    windows = []
    i = 0
    while i < len(word_spans):
        end_idx = min(i + window_words, len(word_spans))
        w_start = word_spans[i][0]
        w_end = word_spans[end_idx - 1][1]
        chunk = text[w_start:w_end]
        tokens = [t.lower() for t in _re.split(r"\W+", chunk) if t]
        windows.append({"start": w_start, "end": w_end, "tokens": tokens})
        if end_idx >= len(word_spans):
            break
        i += stride_words

    if not windows:
        return None, []

    corpus = [w["tokens"] for w in windows]
    index = BM25Okapi(corpus)
    return index, windows


# Cache BM25 indexes keyed by (doi, text length) so repeated searches are fast.
_bm25_cache: dict[str, tuple] = {}


async def _handle_search_in_article(args: dict) -> list[TextContent]:
    """BM25 keyword search within a cached article's full text."""
    import re as _re

    doi = args["doi"]
    terms = args.get("terms", [])
    context_chars = min(int(args.get("context_chars", 500)), 2000)
    max_matches = min(int(args.get("max_matches_per_term", 3)), 10)

    cached = text_cache.get_cached(doi)
    if not cached and doi.startswith("10.2139/ssrn."):
        # Try the remapped published DOI
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as _src:
                _si_remap = await apis.resolve_ssrn_doi(doi, _src)
            if _si_remap.get("published_doi"):
                cached = text_cache.get_cached(_si_remap["published_doi"])
        except Exception:
            pass
    if not cached:
        return [TextContent(
            type="text",
            text=(
                f"Article not in cache for DOI: {doi}\n"
                "Fetch it first with fetch_fulltext(doi=\"...\") "
                "or search_and_read(), then call search_in_article again."
            ),
        )]

    text = cached.text
    sections = cached.sections or []

    def _section_for_offset(offset: int) -> str | None:
        for sec in reversed(sections):
            if sec["start"] <= offset:
                return sec.get("title") or sec.get("keywords") and ", ".join(sec["keywords"][:3]) or None
        return None

    def _clamp_to_word_boundary(s: str, start: int, end: int) -> tuple[int, int]:
        while start > 0 and not s[start - 1].isspace():
            start -= 1
        while end < len(s) and not s[end].isspace():
            end += 1
        return start, end

    # Build or retrieve BM25 index
    cache_key = f"{doi}:{len(text)}"
    if cache_key not in _bm25_cache:
        bm25_index, bm25_windows = _build_bm25_index(text)
        _bm25_cache[cache_key] = (bm25_index, bm25_windows)
        # Evict old entries to keep memory bounded
        if len(_bm25_cache) > 20:
            oldest = next(iter(_bm25_cache))
            del _bm25_cache[oldest]
    else:
        bm25_index, bm25_windows = _bm25_cache[cache_key]

    # ── Lexical dispersion header ────────────────────────────────────────
    # Divide article into 10 equal segments, count term occurrences per
    # segment, render a visual bar.
    n_segments = 10
    seg_len = max(len(text) // n_segments, 1)
    segments = [text[i * seg_len : (i + 1) * seg_len] for i in range(n_segments)]

    dispersion_lines: list[str] = []
    term_match_counts: dict[str, int] = {}
    term_exact_matches: dict[str, list] = {}  # exact regex matches for snippet phase

    for term in terms[:5]:
        if not term.strip():
            continue
        pat = _re.compile(_re.escape(term), _re.IGNORECASE)
        all_matches = list(pat.finditer(text))
        term_exact_matches[term] = all_matches
        term_match_counts[term] = len(all_matches)

        counts = [len(pat.findall(seg)) for seg in segments]
        max_c = max(counts) if counts else 0
        if max_c == 0:
            bar = ". " * n_segments
        else:
            bar = ""
            for c in counts:
                if c == 0:
                    bar += ". "
                elif c <= max_c // 3:
                    bar += "| "
                elif c <= 2 * max_c // 3:
                    bar += "|| "
                else:
                    bar += "||| "

        dispersion_lines.append(f'  "{term}":{" " * max(1, 30 - len(term))}{bar.strip()}')

    lines: list[str] = [
        f"Search results for DOI: {doi}",
        "=" * 60,
        "",
    ]

    if dispersion_lines:
        lines.append(f"Distribution (10 equal segments, each ~{seg_len:,} chars):")
        lines.extend(dispersion_lines)
        lines.append("")

    total_chars = 0
    max_total = config.max_context_length // 2

    for term in terms[:5]:
        if not term.strip():
            continue

        all_matches = term_exact_matches.get(term, [])
        total_hits = term_match_counts.get(term, 0)

        lines.append(f'"{term}" — {total_hits} match{"es" if total_hits != 1 else ""}:')
        lines.append("")

        if not all_matches and bm25_index is not None:
            # BM25 fallback: find best-scoring windows for this term
            query_tokens = [t.lower() for t in _re.split(r"\W+", term) if t]
            scores = bm25_index.get_scores(query_tokens)
            top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:max_matches]
            best_windows = [
                (bm25_windows[i], scores[i]) for i in top_indices if scores[i] > 0
            ]
            if best_windows:
                lines.append(
                    f"  (no exact matches — showing {len(best_windows)} BM25 "
                    f"best-match window{'s' if len(best_windows) != 1 else ''} "
                    f"for semantic proximity)"
                )
                lines.append("")
                for win, score in best_windows:
                    w_center = (win["start"] + win["end"]) // 2
                    ctx_start = max(0, w_center - context_chars)
                    ctx_end = min(len(text), w_center + context_chars)
                    ctx_start, ctx_end = _clamp_to_word_boundary(text, ctx_start, ctx_end)
                    snippet = text[ctx_start:ctx_end]
                    sec_title = _section_for_offset(ctx_start)
                    sec_note = f" (section: {sec_title})" if sec_title else ""
                    lines.append(
                        f"  [BM25 score {score:.2f}] chars {ctx_start:,}–{ctx_end:,}{sec_note}"
                    )
                    lines.append(f"  ...{snippet}...")
                    lines.append("")
                    total_chars += len(snippet)
            else:
                lines.append("  (no matches — try synonyms or abbreviations)")
                lines.append("")
            continue

        if not all_matches:
            lines.append("  (no matches — try synonyms or abbreviations)")
            lines.append("")
            continue

        shown = 0
        for m in all_matches:
            if shown >= max_matches:
                remaining = total_hits - shown
                lines.append(
                    f"  ... and {remaining} more match{'es' if remaining != 1 else ''} "
                    f"(increase max_matches_per_term to see more)"
                )
                lines.append("")
                break

            match_start = m.start()
            match_end = m.end()

            ctx_start = max(0, match_start - context_chars)
            ctx_end = min(len(text), match_end + context_chars)
            ctx_start, ctx_end = _clamp_to_word_boundary(text, ctx_start, ctx_end)

            snippet = text[ctx_start:ctx_end]
            # Highlight the matched term within the snippet
            rel_start = match_start - ctx_start
            rel_end = match_end - ctx_start
            snippet = (
                snippet[:rel_start]
                + "**" + snippet[rel_start:rel_end] + "**"
                + snippet[rel_end:]
            )

            sec_title = _section_for_offset(match_start)
            sec_note = f" (section: {sec_title})" if sec_title else ""

            lines.append(f"  [{shown + 1}] chars {match_start:,}–{match_end:,}{sec_note}")
            lines.append(f"  ...{snippet}...")
            lines.append("")

            total_chars += len(snippet)
            shown += 1

            if total_chars > max_total:
                lines.append(
                    "[Output truncated — use fewer terms or reduce context_chars.]"
                )
                break

        if total_chars > max_total:
            break

    # Footer hints
    lines += [
        "─" * 60,
        "Hints:",
        f"→ For broader context: fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)",
    ]
    if sections:
        lines.append(
            f"→ For a full section: fetch_fulltext(doi=\"{doi}\", mode=\"section\", section=\"...\")"
        )
    lines.append("→ No matches? Try synonyms, abbreviations, or different word forms.")

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# Batch tool handlers
# ---------------------------------------------------------------------------

async def _handle_batch_sections(args: dict) -> list[TextContent]:
    """Get section listings for multiple papers, fetching uncached ones in parallel."""
    dois = args.get("dois", [])
    use_proxy = args.get("use_proxy", False)

    # Cap at 10 DOIs
    dois = dois[:10]

    if not dois:
        return [TextContent(type="text", text="No DOIs provided.")]

    # Remap any SSRN DOIs to their published versions before fetching
    resolved_dois: list[str] = []
    ssrn_remap_index: dict[str, str] = {}  # original_doi → resolved_doi
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _rc:
        for _doi in dois:
            if _doi.startswith("10.2139/ssrn."):
                try:
                    _remap = await apis.resolve_ssrn_doi(_doi, _rc)
                    if _remap.get("published_doi"):
                        logger.info(
                            "batch_sections: SSRN %s → %s", _doi, _remap["published_doi"]
                        )
                        ssrn_remap_index[_doi] = _remap["published_doi"]
                        resolved_dois.append(_remap["published_doi"])
                        continue
                except Exception as e:
                    logger.debug("batch_sections SSRN remap failed for %s: %s", _doi, e)
            resolved_dois.append(_doi)
    dois = resolved_dois

    # Split into cached and uncached
    cached = {}
    uncached = []
    for doi in dois:
        article = text_cache.get_cached(doi)
        if article:
            cached[doi] = article
        else:
            uncached.append(doi)

    # Fetch uncached papers in parallel
    if uncached:
        fetch_tasks = [
            _handle_fetch_pdf({
                "doi": doi,
                "use_proxy": use_proxy,
                "mode": "full",
            })
            for doi in uncached
        ]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # After fetching, re-check cache for newly fetched articles
        for doi, result in zip(uncached, results):
            if isinstance(result, Exception):
                logger.warning("batch_sections: failed to fetch %s: %s", doi, result)
                continue
            article = text_cache.get_cached(doi)
            if article:
                cached[doi] = article

    # Build the combined output
    lines = []
    cached_count = len(cached)
    fetched_count = len(uncached) - len(set(uncached) - set(cached.keys()))
    lines.append(f"Batch sections for {len(dois)} papers ({cached_count} cached, {fetched_count} fetched)")
    lines.append("=" * 60)
    lines.append("")

    for doi in dois:
        lines.append(f"── {doi} ───────────────────────────────────")

        if doi not in cached:
            # Failed to fetch
            lines.append("⚠ Could not fetch: article not in cache after fetch attempt")
            lines.append("")
            continue

        article = cached[doi]
        meta = article.metadata or {}

        # Build a one-line citation from bibliographic metadata
        title = meta.get("title", "")
        authors = meta.get("authors", [])
        year = meta.get("year", "")
        venue = meta.get("venue", "")

        if title:
            cite_parts = []
            if authors:
                if len(authors) == 1:
                    cite_parts.append(authors[0])
                elif len(authors) == 2:
                    cite_parts.append(f"{authors[0]} & {authors[1]}")
                else:
                    cite_parts.append(f"{authors[0]} et al.")
            if year:
                cite_parts.append(f"({year})")
            cite_line = " ".join(cite_parts)
            if cite_line:
                cite_line += " \u2014 "
            cite_line += title
            if venue:
                cite_line += f". {venue}"
            lines.append(cite_line)

        # Get section detection method
        det = article.section_detection
        det_note = {
            "html_headings": "html_headings",
            "pdf_font_analysis": "pdf_font_analysis",
            "pdf_toc": "pdf_toc",
            "text_heuristic": "text_heuristic",
            "keyword_skeleton": "keyword_skeleton",
            "unknown": "unknown",
        }.get(det, det)
        lines.append(f"Section detection: {det_note}")

        # Get sections and format them (without the per-paper header)
        if not article.sections:
            lines.append("No sections detected")
            lines.append("")
            continue

        # Use the same section formatting logic as _apply_mode_filter
        sec_keywords = content_extractor.keywords_for_sections(article.text, article.sections)
        display_sections = content_extractor.infill_keyword_chunks(article.text, article.sections)

        kw_by_start: dict[int, list[str]] = {
            sec.get("start", 0): kw
            for sec, kw in zip(article.sections, sec_keywords)
        }

        structural_idx = 0
        for entry in display_sections:
            wc = entry.get("word_count", 0)
            start = entry.get("start", 0)
            end = entry.get("end", 0)

            if entry.get("_infill"):
                kw = ", ".join(entry.get("keywords", []))
                title = entry["title"]
                lines.append(f"  {title} chars {start:,}–{end:,} ({wc} words): {kw}")
            else:
                indent = "  " if entry.get("level", 2) == 3 else ""
                kw_list = kw_by_start.get(start, [])
                kw_str = ", ".join(kw_list) if kw_list else ""
                lines.append(f"{indent}[{structural_idx}] {entry['title']}  ({wc} words, chars {start:,}–{end:,})")
                if kw_str:
                    lines.append(f"{indent}    → {kw_str}")
                structural_idx += 1

        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_batch_search(args: dict) -> list[TextContent]:
    """Search for terms across multiple cached papers."""
    dois = args.get("dois", [])
    terms = args.get("terms", [])

    # Cap at 10 DOIs and 5 terms
    dois = dois[:10]
    terms = terms[:5]

    if not dois:
        return [TextContent(type="text", text="No DOIs provided.")]

    if not terms:
        return [TextContent(type="text", text="No search terms provided.")]

    # Check which DOIs are cached
    cached = {}
    uncached = []
    for doi in dois:
        article = text_cache.get_cached(doi)
        if article:
            cached[doi] = article
        else:
            uncached.append(doi)

    # Build the header
    lines = []
    terms_str = ", ".join(f'"{t}"' for t in terms)
    lines.append(f"Cross-paper search: {terms_str}")
    uncached_note = ""
    if uncached:
        uncached_note = f" ({len(uncached)} not cached — use batch_sections to fetch)"
    lines.append(f"Searched {len(dois)} papers{uncached_note}")
    lines.append("=" * 60)
    lines.append("")

    # For each term, count matches per paper
    term_results: dict[str, dict[str, dict]] = {}

    for term in terms:
        term_results[term] = {}
        term_lower = term.lower()

        for doi, article in cached.items():
            text = article.text.lower()
            count = text.count(term_lower)

            if count == 0:
                term_results[term][doi] = {"count": 0, "sections": {}}
                continue

            # Find which sections the matches concentrate in
            sections = article.sections or []
            section_counts: dict[str, int] = {}

            # Simple approach: find match positions and map to sections
            pos = 0
            while True:
                pos = text.find(term_lower, pos)
                if pos == -1:
                    break

                # Find which section this position falls into
                for sec in sections:
                    sec_start = sec.get("start", 0)
                    sec_end = sec.get("end", len(text))
                    if sec_start <= pos < sec_end:
                        sec_title = sec.get("title", "Unknown")
                        section_counts[sec_title] = section_counts.get(sec_title, 0) + 1
                        break

                pos += 1

            term_results[term][doi] = {
                "count": count,
                "sections": section_counts,
            }

    # Format results for each term
    for term in terms:
        lines.append(f'"{term}" — found in {sum(1 for r in term_results[term].values() if r["count"] > 0)} of {len(dois)} papers:')

        for doi in dois:
            if doi in uncached:
                continue  # Skip uncached in the main listing

            result = term_results[term].get(doi, {"count": 0, "sections": {}})
            count = result["count"]
            sections = result["sections"]

            if count > 0:
                # Format section concentration
                if sections:
                    sorted_sections = sorted(sections.items(), key=lambda x: -x[1])
                    sec_str = ", ".join(f"{s[0]} ({s[1]})" for s in sorted_sections[:3])
                    lines.append(f"  {doi} — {count} mentions")
                    lines.append(f"    concentrated in: {sec_str}")
                else:
                    lines.append(f"  {doi} — {count} mentions")
            else:
                lines.append(f"  {doi} — not found")

        lines.append("")

    # Find most relevant paper (highest total matches across all terms)
    paper_totals: dict[str, int] = {}
    paper_distinct: dict[str, int] = {}

    for term, results in term_results.items():
        for doi, result in results.items():
            if result["count"] > 0:
                paper_totals[doi] = paper_totals.get(doi, 0) + result["count"]
                paper_distinct[doi] = paper_distinct.get(doi, 0) + 1

    if paper_totals:
        # Sort by total matches, then by distinct terms
        sorted_papers = sorted(
            paper_totals.items(),
            key=lambda x: (-x[1], -paper_distinct.get(x[0], 0))
        )
        most_relevant = sorted_papers[0][0]
        total_matches = sorted_papers[0][1]

        lines.append(f"Most relevant paper: {most_relevant} ({total_matches} total matches)")
        lines.append("")
        lines.append(f"→ search_in_article(doi=\"{most_relevant}\", terms={terms})")

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_mode_filter(
    cached: "text_cache.CachedArticle",
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> list[TextContent]:
    """Apply a mode filter to a cached article and return formatted text."""
    doi = cached.doi

    # Prepend citation header for all modes except "sections"
    if mode != "sections":
        citation_header = _format_citation_header(doi, cached.metadata)
    else:
        citation_header = ""

    if mode == "sections":
        det = cached.section_detection
        det_note = {
            "html_headings":    "html_headings (high confidence — publisher <h2>/<h3> tags)",
            "pdf_font_analysis": "pdf_font_analysis (reliable — font-size threshold on spans)",
            "text_heuristic":   "text_heuristic (approximate — regex on plain text)",
            "keyword_skeleton": "keyword_skeleton (TF-IDF chunks — no structural headings found)",
            "unknown":          "unknown (migrated cache entry)",
        }.get(det, det)

        if not cached.sections:
            # Generate keyword skeleton as a last-resort navigation fallback
            skeleton = content_extractor.generate_keyword_skeleton(cached.text)
            if skeleton:
                cached = text_cache.put_cached(
                    cached.doi, cached.text, cached.source,
                    sections=skeleton,
                    section_detection="keyword_skeleton",
                    word_count=cached.word_count,
                )
                lines = [
                    f"Document map for DOI: {doi}\n"
                    f"Source: {cached.source}\n"
                    "Navigation: keyword_skeleton (no structural headings detected)\n",
                    "=" * 60 + "\n",
                ]
                total = len(skeleton)
                for chunk in skeleton:
                    kw = ", ".join(chunk.get("keywords", []))
                    wc = chunk.get("word_count", 0)
                    start = chunk.get("start", 0)
                    end = chunk.get("end", 0)
                    n = chunk.get("chunk", 0)
                    lines.append(
                        f"[{n}/{total}]  chars {start:,}–{end:,} ({wc} words): {kw}\n"
                    )
                lines += [
                    "\n",
                    f"→ fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)\n",
                    f"→ search_in_article(doi=\"{doi}\", terms=[\"keyword\"])\n",
                ]
                text = "".join(lines)
            else:
                text = (
                    f"No sections detected for DOI: {doi}\n"
                    f"Section detection: {det_note}\n"
                    "Try mode='full' to read the entire text.\n"
                )

        else:
            # Per-section TF-IDF keywords (structural sections only — infill gets
            # its own local keywords computed inside infill_keyword_chunks).
            sec_keywords = content_extractor.keywords_for_sections(
                cached.text, cached.sections
            )

            # Gap infill: insert keyword chunks into large uncovered spans.
            display_sections = content_extractor.infill_keyword_chunks(
                cached.text, cached.sections
            )
            has_infill = any(s.get("_infill") for s in display_sections)

            effective_det_note = (det_note + " + keyword infill") if has_infill else det_note

            lines = [
                f"Sections for DOI: {doi}\n"
                f"Source: {cached.source}\n"
                f"Section detection: {effective_det_note}\n",
                "=" * 60 + "\n",
            ]

            # Build an index from section start offset → keyword list so we can
            # look up keywords quickly while iterating display_sections.
            kw_by_start: dict[int, list[str]] = {
                sec.get("start", 0): kw
                for sec, kw in zip(cached.sections, sec_keywords)
            }

            structural_idx = 0  # counter for non-infill sections only
            for entry in display_sections:
                wc = entry.get("word_count", 0)
                start = entry.get("start", 0)
                end = entry.get("end", 0)

                if entry.get("_infill"):
                    kw = ", ".join(entry.get("keywords", []))
                    title = entry["title"]
                    lines.append(
                        f"  {title} chars {start:,}–{end:,} ({wc} words): {kw}\n"
                    )
                else:
                    indent = "  " if entry.get("level", 2) == 3 else ""
                    kw_list = kw_by_start.get(start, [])
                    kw_str = ", ".join(kw_list) if kw_list else ""
                    lines.append(
                        f"{indent}[{structural_idx}] {entry['title']}  ({wc} words, chars {start:,}–{end:,})\n"
                    )
                    if kw_str:
                        lines.append(f"{indent}    → {kw_str}\n")
                    structural_idx += 1

            lines += [
                "\n",
                f"→ fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)\n",
                f"→ search_in_article(doi=\"{doi}\", terms=[\"keyword\"])\n",
            ]
            text = "".join(lines)

        return [TextContent(type="text", text=citation_header + text)]

    if mode == "section":
        if not section_name:
            return [TextContent(
                type="text",
                text="mode='section' requires the 'section' parameter with a heading name.",
            )]
        if not cached.sections:
            return [TextContent(
                type="text",
                text=(
                    f"No sections detected for DOI: {doi}. "
                    "Use mode='full' to read the entire text."
                ),
            )]
        match = _fuzzy_match_section(section_name, cached.sections)
        if not match:
            available = "\n".join(
                f"  [{i}] {s['title']}" for i, s in enumerate(cached.sections[:20])
            )
            return [TextContent(
                type="text",
                text=(
                    f"Section '{section_name}' not found in DOI: {doi}\n\n"
                    f"Available sections:\n{available}"
                ),
            )]
        end = match.get("end") or len(cached.text)
        section_text = cached.text[match["start"]:end]
        header = (
            f"Section: {match['title']}\n"
            f"DOI: {doi}\n"
            f"Source: {cached.source}\n"
            + "=" * 60 + "\n\n"
        )
        full = header + section_text
        if len(full) > config.max_context_length:
            full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
        return [TextContent(type="text", text=citation_header + full)]

    if mode == "preview":
        lines = [
            f"Preview for DOI: {doi}\nSource: {cached.source}\n",
            "=" * 60 + "\n\n",
        ]
        if not cached.sections:
            # No section data — return first 2 000 chars as a preview
            lines.append(cached.text[:2000])
            if len(cached.text) > 2000:
                lines.append(f"\n\n[... {len(cached.text) - 2000} more characters — use mode='full' ...]")
        else:
            abstract_sec = next(
                (s for s in cached.sections if "abstract" in s["title"].lower()), None
            )
            if abstract_sec:
                end = abstract_sec.get("end") or len(cached.text)
                lines.append(f"## {abstract_sec['title']}\n")
                lines.append(cached.text[abstract_sec["start"]:end])
                lines.append("\n\n")
            else:
                # Show pre-first-heading preamble (likely abstract)
                first_start = cached.sections[0]["start"] if cached.sections else len(cached.text)
                if first_start > 0:
                    lines.append(cached.text[:first_start])
                    lines.append("\n\n")
            for sec in cached.sections:
                if sec == abstract_sec:
                    continue
                end = sec.get("end") or len(cached.text)
                section_text = cached.text[sec["start"]:end]
                words = section_text.split()
                lines.append(f"## {sec['title']}\n")
                lines.append(" ".join(words[:200]))
                remaining = len(words) - 200
                if remaining > 0:
                    lines.append(
                        f"\n[... {remaining} more words — use mode='section', "
                        f"section='{sec['title']}' to read in full ...]\n"
                    )
                lines.append("\n\n")
        text = "".join(lines)
        if len(text) > config.max_context_length:
            text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
        return [TextContent(type="text", text=citation_header + text)]

    if mode == "range":
        start = range_start or 0
        end = range_end or min(start + config.max_context_length, len(cached.text))
        snippet = cached.text[start:end]
        header = (
            f"Character range [{start}:{end}] for DOI: {doi}\n"
            f"Source: {cached.source}\n"
            + "=" * 60 + "\n\n"
        )
        return [TextContent(type="text", text=citation_header + header + snippet)]

    # mode == "full" (default)
    header = (
        f"Full text (cached) for DOI: {doi}\n"
        f"Source: {cached.source}\n"
        + "=" * 60 + "\n\n"
    )
    full = header + cached.text
    if len(full) > config.max_context_length:
        full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return [TextContent(type="text", text=citation_header + full)]


def _fuzzy_match_section(query: str, sections: list[dict]) -> dict | None:
    """Return the best section match for *query* against section titles.

    Tries exact match, then substring (query in title), then reverse
    substring (title in query), to handle queries like "methods" matching
    "Materials and Methods".
    """
    q = query.lower().strip()
    for sec in sections:
        if sec["title"].lower().strip() == q:
            return sec
    for sec in sections:
        if q in sec["title"].lower():
            return sec
    for sec in sections:
        if sec["title"].lower() in q:
            return sec
    return None


def _cache_pdf_and_return(
    pdf_source: "Path | bytes",
    doi: str,
    source: str,
    pages_str: str | None,
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> list[TextContent]:
    """Extract PDF text, write to article cache, apply mode filter, and return.

    When *pages_str* is set we return a partial extraction and skip caching
    (partial text is not useful for section-based access).
    """
    def _append_import_hint(contents: list[TextContent]) -> list[TextContent]:
        hint = zotero_import.get_auto_import_hint(doi)
        if not hint:
            return contents
        out: list[TextContent] = []
        for c in contents:
            if c.type == "text" and hint not in c.text:
                out.append(TextContent(type="text", text=c.text + "\n\n" + hint))
            else:
                out.append(c)
        return out

    if pages_str:
        return _format_extracted_pdf(pdf_source, doi, source, pages_str)

    if config.use_pymupdf4llm:
        result = pdf_extractor.extract_text_pymupdf4llm(pdf_source)
    else:
        result = pdf_extractor.extract_text_with_sections(pdf_source)
    raw_text = result["text"]

    # Build metadata from PDF extraction result
    pdf_meta: dict = {}
    if result["metadata"]:
        pdf_meta["title"] = result["metadata"].get("title", "")
        # Authors may be in the PDF metadata
        if result["metadata"].get("author"):
            pdf_meta["authors"] = result["metadata"]["author"].split(",") if isinstance(result["metadata"]["author"], str) else result["metadata"]["author"]

    cached_article = text_cache.put_cached(
        doi, raw_text, source,
        sections=result["sections"],
        section_detection=result.get("section_detection", "pdf_font_analysis"),
        word_count=len(raw_text.split()),
        metadata=pdf_meta,
    )

    # Queue for background Zotero import (non-blocking; only when we have a file)
    if isinstance(pdf_source, Path):
        zotero_import.enqueue_zotero_import(doi, pdf_source, cached_article)
        # Surface startup/probe issues immediately on the same response.
        hint = zotero_import.get_auto_import_hint(doi)
        if hint:
            logger.warning("Auto-import warning for %s: %s", doi, hint)

    if mode != "full":
        return _append_import_hint(
            _apply_mode_filter(cached_article, mode, section_name, range_start, range_end)
        )

    # mode == "full" — format exactly as _format_extracted_pdf does
    header = (
        f"Full text extracted from DOI: {doi}\n"
        f"Source: {source}\n"
        f"Pages: {result['pages']}\n"
        f"Truncated: {result['truncated']}\n"
    )
    if result["metadata"].get("title"):
        header += f"PDF Title: {result['metadata']['title']}\n"
    if result["sections"]:
        header += "Sections: " + ", ".join(
            s["title"] for s in result["sections"][:15]
        ) + "\n"
    header += "\n" + "=" * 60 + "\n\n"

    # Add citation header for full mode
    citation_header = _format_citation_header(doi, pdf_meta)
    full_text = header + raw_text
    if len(full_text) > config.max_context_length:
        full_text = full_text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return _append_import_hint([TextContent(type="text", text=citation_header + full_text)])


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex stores abstracts as inverted indexes — reconstruct them."""
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def _format_extracted_pdf(
    pdf_source: Path | bytes, doi: str, source: str, pages_str: str | None = None,
) -> list[TextContent]:
    """Extract text from a PDF file (Path preferred) and format as a tool response."""
    if pages_str:
        parts = pages_str.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
        extracted_text = pdf_extractor.extract_text_by_pages(pdf_source, start, end)
        return [TextContent(
            type="text",
            text=(
                f"Extracted text from pages {pages_str} of DOI: {doi}\n"
                f"Source: {source}\n\n"
                f"{extracted_text}"
            ),
        )]

    result = pdf_extractor.extract_text(pdf_source)

    header = (
        f"Full text extracted from DOI: {doi}\n"
        f"Source: {source}\n"
        f"Pages: {result['pages']}\n"
        f"Truncated: {result['truncated']}\n"
    )

    if result["metadata"].get("title"):
        header += f"PDF Title: {result['metadata']['title']}\n"

    if result["sections"]:
        header += "Sections: " + ", ".join(
            s["title"] for s in result["sections"][:15]
        ) + "\n"

    header += "\n" + "=" * 60 + "\n\n"

    return [TextContent(type="text", text=header + result["text"])]


# ---------------------------------------------------------------------------
# Zotero-specific handlers
# ---------------------------------------------------------------------------

async def _handle_search_zotero(args: dict) -> list[TextContent]:
    """Search the user's Zotero library (user + all group libraries)."""
    query = args["query"]
    limit = args.get("limit", 10)

    results = await zotero.search_zotero(query, limit=limit)

    if not results:
        return [TextContent(type="text", text=f"No items found in Zotero for '{query}'")]

    text = f"Found {len(results)} items in Zotero for '{query}':\n\n"
    for i, r in enumerate(results):
        # Show library name for group items
        lib_badge = ""
        if r.get("libraryType") == "group" and r.get("libraryName"):
            lib_badge = f"  [📚 {r['libraryName']}]"
        match_badge = ""
        if r.get("_match_type") == "fulltext":
            match_badge = "  [fulltext match]"

        text += f"[{i}] {r.get('title', 'Untitled')}{lib_badge}{match_badge}\n"
        creators = r.get("creators", [])
        if isinstance(creators, list) and creators:
            names = []
            for c in creators[:3]:
                if isinstance(c, dict):
                    name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                    if name:
                        names.append(name)
            if names:
                text += f"    Authors: {', '.join(names)}\n"
        if r.get("date"):
            text += f"    Date: {r['date']}\n"
        if r.get("DOI"):
            text += f"    DOI: {r['DOI']}\n"
        text += f"    Type: {r.get('itemType', '?')}\n"
        if r.get("abstractNote"):
            abstract = r['abstractNote']
            if len(abstract) > 200:
                abstract = abstract[:200] + "..."
            text += f"    Abstract: {abstract}\n"
        if r.get("DOI"):
            text += f"    → fetch_fulltext(doi=\"{r['DOI']}\") to read\n"
        text += "\n"

    # Opportunistic embedding: during an active index build, pre-warm items
    # the user is actively looking at so they appear in semantic_search_zotero
    # without waiting for the background sync to reach them.
    try:
        from .semantic_index import get_semantic_index
    except ImportError:
        from academic_mcp.semantic_index import get_semantic_index
    try:
        _idx = get_semantic_index()
        _sem_st = _idx._load_status()
        if _sem_st.get("in_progress"):
            _col = _idx._get_chroma_collection()
            for r in results[:5]:  # cap to avoid runaway latency
                _key = r.get("key") or ""
                if not _key:
                    continue
                _ex = _col.get(where={"item_key": _key}, include=[])
                if not _ex.get("ids"):
                    try:
                        await _idx.embed_item_now(_key)
                        logger.debug("hot-path embed completed for %s", _key)
                    except Exception as _e:
                        logger.debug("hot-path embed failed for %s: %s", _key, _e)
    except Exception:
        pass  # never let opportunistic embedding break the search response

    return [TextContent(type="text", text=text)]


async def _handle_search_by_doi(args: dict) -> list[TextContent]:
    """Search Zotero by DOI — instant via SQLite, works across all libraries."""
    doi = args["doi"]

    # Try SQLite first (instant)
    if zotero_sqlite.sqlite_config.available:
        result = await zotero_sqlite.search_by_doi(doi)
        if result:
            text = f"Found in Zotero (via SQLite):\n\n"
            text += f"Title: {result.title or 'Untitled'}\n"
            text += f"DOI: {result.DOI}\n"
            text += f"Library: {result.libraryName}"
            if result.libraryType == "group":
                text += " (group library)"
            text += "\n"
            text += f"Type: {result.itemType or '?'}\n"
            text += f"Date: {result.date}\n"

            if result.creators:
                names = [c.display_name for c in result.creators[:5]]
                text += f"Authors: {', '.join(n for n in names if n)}\n"

            if result.abstractNote:
                text += f"\nAbstract:\n{result.abstractNote}\n"

            if result.DOI:
                text += f"\n→ fetch_fulltext(doi=\"{result.DOI}\") to read full text\n"
            return [TextContent(type="text", text=text)]

    # Fallback to DOI index
    item = await zotero.find_item_by_doi(doi)
    if item:
        data = item.get("data", item)
        text = f"Found in Zotero:\n\n"
        text += f"Title: {data.get('title', 'Untitled')}\n"
        text += f"DOI: {data.get('DOI', doi)}\n"
        return [TextContent(type="text", text=text)]

    return [TextContent(
        type="text",
        text=f"DOI '{doi}' not found in any Zotero library.\n"
             "Use search_papers to find it in external databases.",
    )]


async def _handle_list_libraries(args: dict) -> list[TextContent]:
    """List all Zotero libraries (user + groups)."""
    if not zotero_sqlite.sqlite_config.available:
        return [TextContent(
            type="text",
            text="SQLite access not configured. Set ZOTERO_SQLITE_PATH to your zotero.sqlite file.\n"
                 "Default location: ~/Zotero/zotero.sqlite",
        )]

    libraries = await zotero_sqlite.list_libraries()
    if not libraries:
        return [TextContent(type="text", text="No libraries found in the database.")]

    text = f"Zotero Libraries ({len(libraries)} total):\n\n"
    for lib in libraries:
        icon = "📚" if lib.type == "group" else "👤"
        text += f"  {icon} {lib.name}\n"
        text += f"     Type: {lib.type}\n"
        text += f"     Items: {lib.itemCount}\n"
        if lib.groupID:
            text += f"     Group ID: {lib.groupID}\n"
        text += "\n"

    return [TextContent(type="text", text=text)]


async def _handle_refresh_zotero_index(args: dict) -> list[TextContent]:
    """Rebuild the DOI -> item key index from Zotero."""
    zotero.invalidate_doi_index()
    index = await zotero.get_doi_index()
    count = len(index)

    # Also run connection diagnostics
    status = await zotero.check_connections()

    text = f"Zotero DOI index rebuilt: {count} DOIs indexed.\n"
    text += f"Index cached to: {zotero.zot_config.doi_index_path}\n\n"
    text += "Connection status:\n"
    for backend, info in status.items():
        configured = info.get("configured", False)
        reachable = info.get("reachable", False)
        marker = "OK" if reachable else ("FAILED" if configured else "not configured")
        text += f"  {backend}: {marker}"
        if info.get("host"):
            text += f" ({info['host']})"
        if info.get("path"):
            text += f" ({info['path']})"
        if info.get("total_items"):
            text += f" — {info['total_items']} items"
        if info.get("groups"):
            text += f", {info['groups']} group libraries"
        text += "\n"

    if status.get("sqlite", {}).get("reachable"):
        text += "\n✓ SQLite backend active — fastest path for search and DOI lookup.\n"
        text += "  Searches ALL libraries (user + groups) automatically.\n"

    if not status["local_api"]["reachable"] and zotero.zot_config.local_enabled:
        text += "\nLocal API not reachable. Make sure:\n"
        text += "  - Zotero 7/8 is running\n"
        text += "  - Settings > Advanced > 'Allow other applications...' is checked\n"
        if zotero.zot_config.local_host != "localhost":
            text += f"  - SSH tunnel is open: ssh -L {zotero.zot_config.local_port}:localhost:{zotero.zot_config.local_port} user@{zotero.zot_config.local_host}\n"
        else:
            text += "  - For remote Zotero: set ZOTERO_LOCAL_HOST and SSH tunnel port 23119\n"

    return [TextContent(type="text", text=text)]

