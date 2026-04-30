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

_semantic_empty_hint_shown: bool = False

try:
    from .core.background import _ensure_semantic_background_sync
    from .core import background as _core_bg
except ImportError:
    from academic_mcp.core.background import _ensure_semantic_background_sync
    from academic_mcp.core import background as _core_bg


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="search_papers",
        description=(
            "Search for academic papers across Zotero, Semantic Scholar, and OpenAlex. "
            "Returns a ranked list with metadata, abstracts, and retrieval options.\n\n"
            "QUERY TIPS: Write a hybrid query — a few technical keywords AND a short "
            "natural-language clause stating the question or scope. The lexical sources "
            "match on keywords; the semantic embedder and cross-encoder reranker score "
            "the full intent. "
            "Example: 'How do bees navigate using magnetic fields?' → "
            "'magnetoreception honeybee navigation geomagnetic — how bees orient using magnetic fields'. "
            "Avoid pure prose ('How do bees…?') and avoid keyword soup ('bee magnetic') — "
            "the hybrid form serves both retrieval pipelines. "
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
                        "Search query. The system runs lexical sources (Semantic Scholar, "
                        "OpenAlex, Zotero) AND semantic sources (ChromaDB embeddings + "
                        "cross-encoder rerank) over the same string, so write queries that "
                        "serve both: a few precise technical keywords plus a short clause "
                        "stating intent or scope. "
                        "Example: 'magnetoreception honeybee navigation — how do bees use "
                        "magnetic fields to orient'. "
                        "The keywords drive lexical recall; the trailing clause gives the "
                        "semantic embedder and reranker enough signal to score relevance "
                        "well. Avoid pure prose questions ('How do bees…?') — the keyword "
                        "head still matters. Avoid keyword-only soup ('bee magnetic') — the "
                        "reranker scores worse without context. "
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
            "QUERY TIPS: Write a hybrid query — keywords plus a short clause stating "
            "intent. Both retrieval pipelines (lexical + semantic+reranker) are run. "
            "Example: 'CRISPR gene editing advances — recent breakthroughs in clinical applications'."
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
        from .core import semantic as core_semantic
        from .semantic_index import SemanticIndexUnavailable
    except ImportError:
        from academic_mcp.core import semantic as core_semantic
        from academic_mcp.semantic_index import SemanticIndexUnavailable

    try:
        unique_hits = await core_semantic.semantic_search_zotero(query, k=k)
    except SemanticIndexUnavailable as e:
        return [TextContent(type="text", text=str(e))]

    if not unique_hits:
        return [TextContent(type="text", text="No semantic hits found. Build the index with semantic_index_rebuild first.")]

    lines = [f"Semantic Zotero hits for: {query}", "=" * 50]
    for i, h in enumerate(unique_hits, start=1):
        lines.append(f"[{i}] {h.title or '(untitled)'}")
        score_str = f"score={h.score:.4f}"
        if h.rerank_score is not None:
            score_str += f" | rerank={h.rerank_score:.4f}"
        lines.append(f"    key={h.item_key} | {score_str}")
        if h.doi:
            lines.append(f"    DOI: {h.doi}")
        if h.chunk_source == "ft_cache":
            lines.append(
                f"    chunk: chars {h.char_start or 0}–{h.char_end or 0} "
                f"(chunk {(h.chunk_idx or 0)+1}/{h.chunk_count or 1})"
            )
            lines.append(
                f"    → fetch_fulltext(doi='{h.doi or ''}', mode='range', "
                f"start={h.char_start or 0}, end={h.char_end or 0})"
            )
        if h.snippet:
            lines.append(f"    {h.snippet[:220]}")
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

    global _semantic_empty_hint_shown
    _semantic_empty_hint_shown = False

    if _core_bg._semantic_sync_task and not _core_bg._semantic_sync_task.done():
        if force_rebuild:
            _core_bg._semantic_sync_task.cancel()
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

    _core_bg._semantic_sync_task = asyncio.create_task(_runner())

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
    """Thin wrapper: delegates to core.search.search_papers."""
    try:
        from .core import search as core_search
    except ImportError:
        from academic_mcp.core import search as core_search

    return await core_search.search_papers(
        query=args["query"],
        limit=min(args.get("limit", 5), 20),
        source=args.get("source", "all"),
        start_year=args.get("start_year"),
        end_year=args.get("end_year"),
        venue=args.get("venue"),
        domain_hint=args.get("domain_hint", "general"),
        include_scite=bool(args.get("include_scite", False)),
        semantic=args.get("semantic"),
    )


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

    # ── Format output as RAG-friendly text ────────────────────────
    if not results:
        return [TextContent(type="text", text=f"No papers found for '{query}'.")]

    text = f"Found {len(results)} papers for '{query}':\n"

    # Enrichment status block — shown when caller requested scite or semantic.
    if include_scite or use_semantic:
        _status_parts: list[str] = []
        if include_scite:
            _with_doi = sum(1 for r in results if r.doi)
            _enriched = sum(1 for r in results if r.scite)
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
        if r.in_zotero:
            badges.append("★ IN ZOTERO")
        if r.has_oa_pdf:
            badges.append("OA")
        if r.scite and r.scite.retracted:
            badges.append("RETRACTED?")
        _wt = (r.work_type or "").lower()
        if _wt == "book-chapter":
            badges.append("CHAPTER")
        elif _wt in ("book", "edited-book", "monograph", "reference-book"):
            badges.append("BOOK")
        badge_str = f"  [{', '.join(badges)}]" if badges else ""
        text += f"[{i}] {r.title}{badge_str}\n"

        # Metadata
        text += f"    Authors: {_authors_str(r.authors)}\n"
        if r.year:
            text += f"    Year: {r.year}"
            if r.citations:
                text += f"  |  Citations: {r.citations}"
            text += "\n"
        if r.venue:
            law_note = "  [law review — via Primo/HeinOnline]" if "primo_law" in r.found_in else ""
            text += f"    Venue: {r.venue}{law_note}\n"
        if _wt == "book-chapter" and r.container_title and r.container_title != r.venue:
            text += f"    In book: {r.container_title}\n"
        if r.doi:
            text += f"    DOI: {r.doi}\n"
        text += f"    Sources: {', '.join(r.found_in)}\n"
        if r.semantic_similarity is not None:
            text += f"    Relevance: {r.semantic_similarity:.3f}\n"
        if r.semantic_zotero_score is not None:
            text += f"    Semantic Zotero score: {r.semantic_zotero_score:.3f}\n"
        if r.scite:
            s = r.scite
            text += (
                "    Scite: "
                f"citing={s.citing} | "
                f"supporting={s.supporting} | "
                f"contrasting={s.contrasting} | "
                f"mentioning={s.mentioning}"
            )
            if s.retracted:
                text += "  [retraction/correction signal]"
            text += "\n"

        # Abstract / Preview
        abstract = r.abstract or ""
        if abstract:
            # Truncate long abstracts for the listing
            if len(abstract) > 400:
                abstract = abstract[:400] + "..."
            text += f"\n    {abstract}\n"

        # URL line — shown only for DOI-less items so the LLM can pass it to
        # fetch_fulltext.  When a DOI is present it's already the canonical handle.
        if r.url and not r.doi:
            text += f"    URL: {r.url}\n"

        # Follow-up action guidance
        text += "\n    → "
        if r.doi:
            if r.in_zotero:
                text += f"Full text available. Call fetch_fulltext(doi=\"{r.doi}\", mode=\"sections\") to explore."
            elif r.primo_oa_url:
                text += f"Open access via library. Call fetch_fulltext(doi=\"{r.doi}\", mode=\"sections\") to explore."
            elif r.has_oa_pdf:
                text += f"Open access PDF available. Call fetch_fulltext(doi=\"{r.doi}\", mode=\"sections\") to explore."
            elif r.primo_proxy_url:
                text += f"Available via institutional access: {r.primo_proxy_url}"
            else:
                text += f"May need proxy. Call fetch_fulltext(doi=\"{r.doi}\", use_proxy=true, mode=\"sections\") to explore."
        elif r.url:
            text += (
                f"No DOI, but URL available. "
                f"Call fetch_fulltext(url=\"{r.url}\", mode=\"sections\") to explore."
            )
        elif r.in_zotero and r.zotero_key:
            text += (
                f"No DOI, but in Zotero. Call fetch_fulltext(zotero_key=\"{r.zotero_key}\", "
                "mode=\"sections\") to explore."
            )
        else:
            text += "No DOI — full text retrieval not available for this result."

        # Expansion hint: for promising older papers, nudge toward citation-graph
        # exploration — often more productive than another keyword search.
        try:
            _yr = int(r.year) if r.year else None
        except (TypeError, ValueError):
            _yr = None
        _sim = r.semantic_similarity
        _cites = r.citations or 0
        _looks_strong = (i == 0) or (isinstance(_sim, (int, float)) and _sim >= 0.3) or _cites >= 20
        if r.doi and _yr and _yr <= _current_year - 1 and _looks_strong:
            text += (
                f"\n    ⇢ Promising + {_current_year - _yr}yr old: consider "
                f"get_citations(doi=\"{r.doi}\") to see what built on it. "
                "Pass exclude_dois=[the DOIs below] to skip results you've already seen, "
                "and use BROADER keywords than this query to pull in adjacent work."
            )
        # Book/chapter navigation hints
        if r.doi and _wt == "book-chapter":
            text += (
                f"\n    ⇢ Book chapter: get_book_chapters(doi=\"{r.doi}\") lists "
                "the sibling chapters in the same volume — often a richer topical "
                "neighbourhood than keyword search."
            )
        elif r.doi and _wt in ("book", "edited-book", "monograph", "reference-book"):
            text += (
                f"\n    ⇢ Book: get_book_chapters(doi=\"{r.doi}\") drills down into "
                "the individual chapters (each has its own DOI and can be fetched separately). "
                "Prefer this to fetching the whole book — chapter PDFs are usually the only "
                "thing publishers host, and sectioning works much better per-chapter."
            )
        text += "\n\n"

    # Footer: ready-to-paste exclude_dois list for citation-based widening.
    _result_dois = [r.doi for r in results if r.doi]
    if _result_dois:
        text += "─" * 60 + "\n"
        text += "To widen via citations without repeats, copy this list as exclude_dois:\n"
        text += json.dumps(_result_dois) + "\n"

    return [TextContent(type="text", text=text)]


async def _handle_get_paper(args: dict) -> list[TextContent]:
    identifier = args["identifier"]

    try:
        from .core import paper as core_paper
    except ImportError:
        from academic_mcp.core import paper as core_paper

    info = await core_paper.get_paper(identifier)
    doi = info.doi or identifier

    text = f"Paper: {info.title}\n"
    text += f"DOI: {doi}\n"

    if info.s2_id:
        text += f"\nSemantic Scholar ID: {info.s2_id}\n"
        text += f"Authors: {', '.join(info.authors)}\n"
        text += f"Year: {info.year}\n"
        text += f"Venue: {info.venue}\n"
        text += f"Citations: {info.citation_count}\n"
        text += f"References: {info.reference_count}\n"
        if info.tldr:
            text += f"\nTL;DR: {info.tldr}\n"
        if info.abstract:
            text += f"\nAbstract:\n{info.abstract}\n"
    elif info.authors:
        text += f"\nAuthors: {', '.join(info.authors)}\n"
        text += f"Year: {info.year}\n"
        text += f"Citations: {info.citation_count}\n"
        if info.abstract:
            text += f"\nAbstract:\n{info.abstract}\n"

    # Book / chapter drill hint
    oa_type = info.oa_type or ""
    if oa_type in _CHAPTER_TYPES:
        text += "\nType: book chapter"
        if info.oa_container:
            text += f" in \"{info.oa_container}\""
        text += f"\n→ See sibling chapters: get_book_chapters(doi=\"{doi}\")\n"
    elif oa_type in _BOOK_TYPES:
        text += (
            "\nType: book\n"
            f"→ List chapters: get_book_chapters(doi=\"{doi}\") — fetch individual "
            "chapters rather than the whole book PDF.\n"
        )

    if info.pdf_urls:
        text += "\nAvailable PDF URLs:\n"
        for p in info.pdf_urls:
            text += f"  [{p.source}] {p.url}\n"
    else:
        text += "\nNo open access PDF URLs found.\n"
        if not info.is_oa:
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


def _format_citations_result(result) -> str:
    """Format a core.citations.CitationsResult for LLM consumption."""
    from .core.types import CitationsResult  # local import to avoid top-level circular import

    if not result.items:
        return (
            f"No {result.direction} found for DOI: {result.doi}\n"
            f"(Total count from OpenAlex: {result.total})"
        )

    lines = [
        f"{result.direction.title()} for DOI: {result.doi}",
        f"Showing {len(result.items)} of {result.total:,} total",
        "=" * 60,
        "",
    ]
    for i, item in enumerate(result.items):
        authors_str = ", ".join(n for n in item.authors[:4] if n)
        if len(item.authors) > 4:
            authors_str += f" +{len(item.authors) - 4} more"

        lines.append(f"[{i}] {item.title or 'Untitled'}")
        if item.in_zotero:
            lines.append("    ★ IN ZOTERO")
        if authors_str:
            lines.append(f"    Authors: {authors_str}")
        lines.append(f"    Year: {item.year or '?'}")
        if item.venue:
            lines.append(f"    Venue: {item.venue}")
        lines.append(f"    Citations: {item.cited_by_count:,}")
        if item.doi:
            lines.append(f"    DOI: {item.doi}")
        if item.openalex_id:
            lines.append(f"    OpenAlex: {item.openalex_id}")
        if item.abstract:
            abstract = item.abstract
            if len(abstract) > 300:
                abstract = abstract[:300] + "..."
            lines.append(f"    Abstract: {abstract}")
        if item.doi:
            lines.append(f"    → fetch_fulltext(doi=\"{item.doi}\") to read")
        lines.append("")

    sample = [item.doi for item in result.items[:5] if item.doi]
    if sample:
        lines.append("─" * 60)
        lines.append("Next steps:")
        lines.append(f"→ batch_sections(dois={json.dumps(sample)}) to survey these papers")
        lines.append("→ get_citations / get_references on any result to continue exploring the graph")
        if result.direction == "citations":
            lines.append(f"→ get_references(doi=\"{result.doi}\") to also see what this paper cites")
        else:
            lines.append(f"→ get_citations(doi=\"{result.doi}\") to also see what cites this paper")

    return "\n".join(lines)


async def _handle_get_citations(args: dict) -> list[TextContent]:
    """Find forward citations (papers that cite the given DOI)."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 25), 50)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")

    try:
        from .core import citations as core_citations
    except ImportError:
        from academic_mcp.core import citations as core_citations

    result = await core_citations.get_citations(
        doi,
        keywords=keywords,
        limit=limit,
        start_year=start_year,
        end_year=end_year,
        openalex_id=openalex_id,
        exclude_dois=args.get("exclude_dois"),
    )
    if result.error:
        return [TextContent(type="text", text=f"Error fetching citations for {doi}: {result.error}")]
    text = _format_citations_result(result)
    if result.dropped:
        text += f"\n\n(Filtered out {result.dropped} result(s) matching exclude_dois.)"
    return [TextContent(type="text", text=text)]


async def _handle_get_references(args: dict) -> list[TextContent]:
    """Find backward references (papers cited by the given DOI)."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 25), 50)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")

    try:
        from .core import citations as core_citations
    except ImportError:
        from academic_mcp.core import citations as core_citations

    result = await core_citations.get_references(
        doi,
        keywords=keywords,
        limit=limit,
        start_year=start_year,
        end_year=end_year,
        openalex_id=openalex_id,
        exclude_dois=args.get("exclude_dois"),
    )
    if result.error:
        return [TextContent(type="text", text=f"Error fetching references for {doi}: {result.error}")]
    text = _format_citations_result(result)
    if result.dropped:
        text += f"\n\n(Filtered out {result.dropped} result(s) matching exclude_dois.)"
    return [TextContent(type="text", text=text)]


async def _handle_get_citation_tree(args: dict) -> list[TextContent]:
    """Get both citations and references concurrently."""
    doi = args["doi"]
    keywords = args.get("keywords")
    limit = min(args.get("limit", 10), 25)
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    openalex_id = args.get("openalex_id")

    try:
        from .core import citations as core_citations
    except ImportError:
        from academic_mcp.core import citations as core_citations

    tree = await core_citations.get_citation_tree(
        doi,
        keywords=keywords,
        limit=limit,
        start_year=start_year,
        end_year=end_year,
        openalex_id=openalex_id,
        exclude_dois=args.get("exclude_dois"),
    )

    parts = []
    if tree.citations:
        if tree.citations.error:
            parts.append(f"⚠ Citations lookup failed: {tree.citations.error}")
        else:
            parts.append(_format_citations_result(tree.citations))
            if tree.citations.dropped:
                parts.append(f"(Filtered out {tree.citations.dropped} citation(s) matching exclude_dois.)")

    parts.append("\n" + "═" * 60 + "\n")

    if tree.references:
        if tree.references.error:
            parts.append(f"⚠ References lookup failed: {tree.references.error}")
        else:
            parts.append(_format_citations_result(tree.references))
            if tree.references.dropped:
                parts.append(f"(Filtered out {tree.references.dropped} reference(s) matching exclude_dois.)")

    return [TextContent(type="text", text="\n".join(parts))]


_BOOK_TYPES = {"book", "edited-book", "monograph", "reference-book"}
_CHAPTER_TYPES = {"book-chapter", "reference-entry", "book-part", "book-section"}


async def _handle_get_book_chapters(args: dict) -> list[TextContent]:
    """List chapters sharing an ISBN or book title — drill up/down between book and chapters."""
    raw_doi = (args.get("doi") or "").strip()
    raw_isbn = (args.get("isbn") or "").strip()
    keywords = args.get("keywords")
    limit = min(max(args.get("limit", 50), 1), 100)

    if not raw_doi and not raw_isbn:
        return [TextContent(type="text", text="Provide either 'doi' or 'isbn'.")]

    try:
        from .core import citations as core_citations
    except ImportError:
        from academic_mcp.core import citations as core_citations

    result = await core_citations.get_book_chapters(
        doi=raw_doi, isbn=raw_isbn, keywords=keywords, limit=limit
    )

    if result.error:
        return [TextContent(type="text", text=result.error)]

    items = result.items
    seed_doi = result.seed_doi
    seed_type = result.seed_type

    lines = []
    if seed_type in _CHAPTER_TYPES and seed_doi:
        lines.append(f"Drilled UP from chapter {seed_doi} → sibling chapters in:")
    elif seed_type in _BOOK_TYPES and seed_doi:
        lines.append(f"Drilled DOWN from book {seed_doi} → chapters:")
    else:
        lines.append("Chapters matching the provided identifier:")
    if result.book_title:
        lines.append(f"  Book: {result.book_title}")
    if result.isbns:
        lines.append(f"  ISBN: {', '.join(result.isbns)}")
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

    dois = [it.get("DOI") for it in items if it.get("DOI")]
    if dois:
        lines.append("─" * 60)
        lines.append("Next steps:")
        lines.append("→ batch_sections(dois=[...]) to survey several chapters in parallel")
        if keywords is None:
            lines.append("→ Re-call with keywords='...' to narrow to chapters on a specific subtopic")

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# MCP formatter — converts FetchedArticle structured fields → TextContent
# ---------------------------------------------------------------------------

def _format_citation_header(doi: str, metadata: dict | None = None) -> str:
    """Format a short citation block for prepending to article text."""
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


def _format_sections_index(fa) -> str:
    """Render the sections index from fa.available_sections."""
    from .core.types import FetchMode
    doi = fa.doi
    sections = fa.available_sections

    det = fa.section_detection
    det_note = {
        "html_headings":     "html_headings (high confidence — publisher <h2>/<h3> tags)",
        "pdf_font_analysis": "pdf_font_analysis (reliable — font-size threshold on spans)",
        "text_heuristic":    "text_heuristic (approximate — regex on plain text)",
        "keyword_skeleton":  "keyword_skeleton (TF-IDF chunks — no structural headings found)",
        "unknown":           "unknown (migrated cache entry)",
    }.get(det, det)

    if det == "keyword_skeleton":
        lines = [
            f"Document map for DOI: {doi}\n"
            f"Source: {fa.source}\n"
            "Navigation: keyword_skeleton (no structural headings detected)\n",
            "=" * 60 + "\n",
        ]
        for sec in sections:
            kw = ", ".join(sec.keywords)
            lines.append(
                f"{sec.title}  chars {sec.char_start:,}–{sec.char_end:,}"
                f" ({sec.word_count} words): {kw}\n"
            )
    elif not sections:
        return (
            f"No sections detected for DOI: {doi}\n"
            f"Section detection: {det_note}\n"
            "Try mode='full' to read the entire text.\n"
        )
    else:
        has_infill = any(s.is_infill for s in sections)
        effective_det_note = (det_note + " + keyword infill") if has_infill else det_note
        lines = [
            f"Sections for DOI: {doi}\n"
            f"Source: {fa.source}\n"
            f"Section detection: {effective_det_note}\n",
            "=" * 60 + "\n",
        ]
        structural_idx = 0
        for sec in sections:
            wc = sec.word_count
            start = sec.char_start
            end = sec.char_end
            if sec.is_infill:
                kw = ", ".join(sec.keywords)
                lines.append(
                    f"  {sec.title} chars {start:,}–{end:,} ({wc} words): {kw}\n"
                )
            else:
                indent = "  " if sec.level == 3 else ""
                kw_str = ", ".join(sec.keywords) if sec.keywords else ""
                lines.append(
                    f"{indent}[{structural_idx}] {sec.title}  ({wc} words, chars {start:,}–{end:,})\n"
                )
                if kw_str:
                    lines.append(f"{indent}    → {kw_str}\n")
                structural_idx += 1

    lines += [
        "\n",
        f"→ fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)\n",
        f"→ search_in_article(doi=\"{doi}\", terms=[\"keyword\"])\n",
    ]
    return "".join(lines)


def _format_single_section(fa) -> str:
    """Render a matched section. fa.text must contain the raw section body."""
    doi = fa.doi
    sec = fa.matched_section
    if sec is None:
        # Error: no section matched — fa.text has the error message
        return fa.text
    header = (
        f"Section: {sec.title}\n"
        f"DOI: {doi}\n"
        f"Source: {fa.source}\n"
        + "=" * 60 + "\n\n"
    )
    full = header + fa.text
    if len(full) > config.max_context_length:
        full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return full


def _format_preview(fa) -> str:
    """Render preview from fa.preview_chunks."""
    doi = fa.doi
    lines = [
        f"Preview for DOI: {doi}\nSource: {fa.source}\n",
        "=" * 60 + "\n\n",
    ]
    if not fa.preview_chunks:
        lines.append(fa.text[:2000] if fa.text else "")
        if fa.text and len(fa.text) > 2000:
            lines.append(f"\n\n[... {len(fa.text) - 2000} more characters — use mode='full' ...]")
    else:
        for chunk in fa.preview_chunks:
            if chunk.section_title is None:
                # Preamble (pre-first-heading)
                lines.append(chunk.text)
                lines.append("\n\n")
            else:
                lines.append(f"## {chunk.section_title}\n")
                lines.append(chunk.text)
                remaining = chunk.word_count_total - chunk.word_count_shown
                if remaining > 0:
                    lines.append(
                        f"\n[... {remaining} more words — use mode='section', "
                        f"section='{chunk.section_title}' to read in full ...]\n"
                    )
                lines.append("\n\n")
    text = "".join(lines)
    if len(text) > config.max_context_length:
        text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return text


def _format_range(fa) -> str:
    """Render character range from fa.range_chars and fa.text (raw slice)."""
    doi = fa.doi
    if fa.range_chars:
        start, end = fa.range_chars
    else:
        start, end = 0, len(fa.text)
    header = (
        f"Character range [{start}:{end}] for DOI: {doi}\n"
        f"Source: {fa.source}\n"
        + "=" * 60 + "\n\n"
    )
    return header + fa.text


def _format_full_body(fa) -> str:
    """Render full article body from fa.text (raw body) and metadata."""
    doi = fa.doi
    header = (
        f"Full text (cached) for DOI: {doi}\n"
        f"Source: {fa.source}\n"
        + "=" * 60 + "\n\n"
    )
    full = header + fa.text
    if fa.truncated:
        full = full + "\n\n[... TRUNCATED ...]"
    return full


def _format_failure(fa) -> list[TextContent]:
    """Render a failure response from fa.error and fa.failure_hints."""
    text = fa.error or "Unknown error"
    if fa.failure_hints:
        text = text + "\n" + "".join(fa.failure_hints)
    return [TextContent(type="text", text=text)]


def _format_fetched_for_mcp(fa) -> list[TextContent]:
    """Convert a FetchedArticle's structured fields to MCP TextContent."""
    from .core.types import FetchMode
    if fa.error and not fa.failure_hints:
        # Legacy error path: error text is already in fa.text or fa.error
        return [TextContent(type="text", text=fa.text or fa.error or "")]

    if fa.failure_hints:
        return _format_failure(fa)

    parts: list[str] = []
    citation = _format_citation_header(fa.doi, fa.metadata)
    if citation and fa.mode != FetchMode.sections:
        parts.append(citation)

    if fa.mode == FetchMode.sections:
        parts.append(_format_sections_index(fa))
    elif fa.mode == FetchMode.section:
        parts.append(_format_single_section(fa))
    elif fa.mode == FetchMode.preview:
        parts.append(_format_preview(fa))
    elif fa.mode == FetchMode.range:
        parts.append(_format_range(fa))
    else:  # full
        parts.append(_format_full_body(fa))

    # citation already ends with \n\n; mode formatters start without leading \n
    text = "".join(parts)
    if fa.auto_import_status:
        text = text + "\n\n" + fa.auto_import_status

    return [TextContent(type="text", text=text)]


async def _handle_fetch_pdf(args: dict) -> list[TextContent]:
    try:
        from .core import fetch as core_fetch
        from .core.types import ArticleId
    except ImportError:
        from academic_mcp.core import fetch as core_fetch
        from academic_mcp.core.types import ArticleId
    article = await core_fetch.fetch_article(
        ArticleId(
            doi=args.get("doi"),
            zotero_key=args.get("zotero_key"),
            url=args.get("url"),
        ),
        mode=args.get("mode", "sections"),
        section=args.get("section"),
        range_start=args.get("range_start"),
        range_end=args.get("range_end"),
        use_proxy=args.get("use_proxy", False),
        pages=args.get("pages"),
        source=args.get("source", "auto"),
    )
    return _format_fetched_for_mcp(article)

async def _handle_search_and_read(args: dict) -> list[TextContent]:
    query = args["query"]
    result_index = args.get("result_index", 0)
    use_proxy = args.get("use_proxy", False)

    # Use the unified parallel pipeline so semantic Zotero, S2, OpenAlex,
    # and Primo all contribute candidates — not just S2.
    try:
        from .core import search as core_search
    except ImportError:
        from academic_mcp.core import search as core_search

    try:
        results = await core_search.search_papers(
            query=query,
            limit=5,
            source="all",
            semantic=args.get("semantic"),
        )
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
    doi = paper.doi
    title = paper.title or "Untitled"
    sources = ", ".join(paper.found_in) or "?"

    text = f"Selected paper [{result_index}]: {title}\n"
    text += f"Sources: {sources}\n"
    text += f"DOI: {doi}\n\n"

    # If the paper is in Zotero, fetch_fulltext via zotero_key works without a DOI.
    if not doi:
        zot_key = paper.zotero_key
        if zot_key:
            fetch_result = await _handle_fetch_pdf({
                "zotero_key": zot_key,
                "use_proxy": use_proxy,
            })
            return [TextContent(type="text", text=text + fetch_result[0].text)]
        url = paper.url
        if url:
            fetch_result = await _handle_fetch_pdf({
                "url": url,
                "use_proxy": use_proxy,
            })
            return [TextContent(type="text", text=text + fetch_result[0].text)]
        text += "No DOI / Zotero key / URL found for this paper. Cannot fetch full text.\n"
        if paper.abstract:
            text += f"\nAbstract:\n{paper.abstract}\n"
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

    try:
        from .core import paper as core_paper
    except ImportError:
        from academic_mcp.core import paper as core_paper

    result = await core_paper.find_pdf_urls(doi)

    text = f"PDF URL candidates for DOI: {doi}\n\n"

    if result.candidates:
        for i, c in enumerate(result.candidates):
            text += f"  [{i+1}] Source: {c.source}\n"
            text += f"      URL: {c.url}\n\n"
    else:
        text += "No open access PDF URLs found.\n"

    if result.is_oa or result.oa_status or result.journal_is_oa:
        text += f"\nUnpaywall OA status: {'Open Access' if result.is_oa else 'Not OA'}\n"
        if result.oa_status:
            text += f"OA type: {result.oa_status}\n"
        if result.journal_is_oa:
            text += "Journal is fully OA\n"

    return [TextContent(type="text", text=text)]


async def _handle_search_in_article(args: dict) -> list[TextContent]:
    """BM25 keyword search within a cached article's full text."""
    doi = args["doi"]
    terms = args.get("terms", [])
    context_chars = min(int(args.get("context_chars", 500)), 2000)
    max_matches = min(int(args.get("max_matches_per_term", 3)), 10)

    try:
        from .core import in_article as core_in_article
    except ImportError:
        from academic_mcp.core import in_article as core_in_article

    try:
        result = await core_in_article.search_in_article(
            doi, terms, context_chars=context_chars, max_matches=max_matches
        )
    except LookupError:
        return [TextContent(
            type="text",
            text=(
                f"Article not in cache for DOI: {doi}\n"
                "Fetch it first with fetch_fulltext(doi=\"...\") "
                "or search_and_read(), then call search_in_article again."
            ),
        )]

    seg_len = result.segment_length
    sections = result.sections

    lines: list[str] = [
        f"Search results for DOI: {doi}",
        "=" * 60,
        "",
    ]

    # Lexical dispersion header
    dispersion_lines: list[str] = []
    for tr in result.term_results:
        counts = tr.segment_counts
        max_c = max(counts) if counts else 0
        if max_c == 0:
            bar = ". " * 10
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
        dispersion_lines.append(f'  "{tr.term}":{" " * max(1, 30 - len(tr.term))}{bar.strip()}')

    if dispersion_lines:
        lines.append(f"Distribution (10 equal segments, each ~{seg_len:,} chars):")
        lines.extend(dispersion_lines)
        lines.append("")

    for tr in result.term_results:
        lines.append(f'"{tr.term}" — {tr.total_hits} match{"es" if tr.total_hits != 1 else ""}:')
        lines.append("")

        if not tr.matches:
            lines.append("  (no matches — try synonyms or abbreviations)")
            lines.append("")
            continue

        bm25_matches = [m for m in tr.matches if m.is_bm25]
        exact_matches = [m for m in tr.matches if not m.is_bm25]

        if bm25_matches and not exact_matches:
            lines.append(
                f"  (no exact matches — showing {len(bm25_matches)} BM25 "
                f"best-match window{'s' if len(bm25_matches) != 1 else ''} "
                f"for semantic proximity)"
            )
            lines.append("")
            for m in bm25_matches:
                sec_note = f" (section: {m.section})" if m.section else ""
                lines.append(
                    f"  [BM25 score {m.bm25_score:.2f}] chars {m.char_start:,}–{m.char_end:,}{sec_note}"
                )
                lines.append(f"  ...{m.snippet}...")
                lines.append("")
        else:
            total_hits = tr.total_hits
            for i, m in enumerate(exact_matches):
                sec_note = f" (section: {m.section})" if m.section else ""
                lines.append(f"  [{i + 1}] chars {m.char_start:,}–{m.char_end:,}{sec_note}")
                snippet = m.snippet
                if m.match_start is not None and m.match_end is not None:
                    snippet = (
                        snippet[:m.match_start]
                        + "**" + snippet[m.match_start:m.match_end] + "**"
                        + snippet[m.match_end:]
                    )
                lines.append(f"  ...{snippet}...")
                lines.append("")
            shown = len(exact_matches)
            if shown < total_hits:
                remaining = total_hits - shown
                lines.append(
                    f"  ... and {remaining} more match{'es' if remaining != 1 else ''} "
                    f"(increase max_matches_per_term to see more)"
                )
                lines.append("")

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

from .core.search import reconstruct_abstract as _reconstruct_abstract


# ---------------------------------------------------------------------------
# Zotero-specific handlers
# ---------------------------------------------------------------------------

async def _handle_search_zotero(args: dict) -> list[TextContent]:
    """Search the user's Zotero library (user + all group libraries)."""
    query = args["query"]
    limit = args.get("limit", 10)

    try:
        from .core import search as core_search
    except ImportError:
        from academic_mcp.core import search as core_search

    results = await core_search.search_zotero(query, limit=limit)

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

    # Opportunistic embedding is now handled inside core.search.search_zotero.

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

    try:
        from .core import libraries as core_libraries
    except ImportError:
        from academic_mcp.core import libraries as core_libraries

    libraries = await core_libraries.list_libraries()
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
    try:
        from .core import libraries as core_libraries
    except ImportError:
        from academic_mcp.core import libraries as core_libraries

    result = await core_libraries.refresh_zotero_index()
    status = result.connections

    text = f"Zotero DOI index rebuilt: {result.doi_count} DOIs indexed.\n"
    text += f"Index cached to: {result.index_path}\n\n"
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

    if result.sqlite_active:
        text += "\n✓ SQLite backend active — fastest path for search and DOI lookup.\n"
        text += "  Searches ALL libraries (user + groups) automatically.\n"

    if not status.get("local_api", {}).get("reachable") and result.local_enabled:
        text += "\nLocal API not reachable. Make sure:\n"
        text += "  - Zotero 7/8 is running\n"
        text += "  - Settings > Advanced > 'Allow other applications...' is checked\n"
        if result.local_host != "localhost":
            text += f"  - SSH tunnel is open: ssh -L {result.local_port}:localhost:{result.local_port} user@{result.local_host}\n"
        else:
            text += "  - For remote Zotero: set ZOTERO_LOCAL_HOST and SSH tunnel port 23119\n"

    return [TextContent(type="text", text=text)]

