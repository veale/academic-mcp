"""MCP Server — tool definitions and request handlers."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

try:
    from . import apis, content_extractor, pdf_fetcher, pdf_extractor, text_cache, zotero, zotero_sqlite
    from .config import config
    from .reranker import rerank_results
except ImportError:
    from academic_mcp import apis, content_extractor, pdf_fetcher, pdf_extractor, text_cache, zotero, zotero_sqlite
    from academic_mcp.config import config
    from academic_mcp.reranker import rerank_results

logger = logging.getLogger(__name__)
server = Server("academic-research")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="search_papers",
        description=(
            "Search for academic papers across Zotero, Semantic Scholar, and OpenAlex. "
            "Returns a semantically re-ranked list: results are sorted by cosine "
            "similarity to your query (with Zotero items boosted), then by "
            "retrievability, citations, and recency. Each result has metadata, "
            "an abstract or preview, and tells you how to get the full text. "
            "Results are deduplicated by DOI and enriched with Zotero availability.\n\n"
            "USAGE NOTE — Query Expansion: Decompose natural language questions into "
            "2-6 technical keywords before calling. VERBATIM queries are discouraged. "
            "Example: 'How do bees navigate using magnetic fields?' → "
            "'magnetoreception honeybee navigation geomagnetic'. "
            "Use author:LastName to search by author.\n\n"
            "NEXT STEP: after finding a paper, call "
            "fetch_fulltext(doi=..., mode='sections') to see its structure and "
            "keywords before reading further."
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
                    "enum": ["all", "semantic_scholar", "openalex", "zotero", "primo"],
                    "description": (
                        "Which sources to search. 'all' (default) searches Zotero + "
                        "Semantic Scholar + OpenAlex + Primo (if configured) and "
                        "deduplicates. 'zotero' searches only your library. "
                        "'primo' searches your institution's Ex Libris catalogue."
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
            "getting the full text."
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
        name="fetch_fulltext",
        description=(
            "Get the full text of a paper for analysis. "
            "Checks Zotero first (pre-extracted text if available, then PDF), "
            "then tries Unpaywall, Semantic Scholar, OpenAlex, stealth browser, "
            "and optionally institutional proxy. "
            "After the first fetch the text is cached locally, so subsequent calls "
            "are instant local reads.\n\n"
            "RECOMMENDED WORKFLOW for targeted reading:\n"
            "1. Call with mode='sections' first — shows headings with TF-IDF keywords "
            "   revealing what each section discusses. Large gaps between headings are "
            "   automatically filled with keyword-labelled chunks for navigation.\n"
            "2. Call with mode='section' and the heading you need, OR use "
            "   search_in_article to find specific terms across the paper.\n"
            "3. Use mode='range' with character offsets from the sections listing "
            "   or search_in_article results to read specific passages.\n\n"
            "Use mode='full' only when you genuinely need the entire text (e.g. "
            "summarising the whole paper). For answering specific questions, the "
            "sections → search → range workflow uses far less context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the paper (from search_papers results)",
                },
                "use_proxy": {
                    "type": "boolean",
                    "description": "Route through institutional proxy if configured",
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
                        "'sections' — lists headings with TF-IDF keywords showing what each "
                        "section discusses; gaps between sparse headings are automatically "
                        "filled with keyword-labelled chunks. CALL THIS FIRST. "
                        "'section' — returns a specific section by name (fuzzy-matched). "
                        "'preview' — returns abstract + first paragraph of each section. "
                        "'range' — returns a character slice (set range_start/range_end from "
                        "the offsets shown in sections output or search_in_article results). "
                        "'full' — returns everything; can be 50,000+ characters for a typical "
                        "journal article — avoid unless you need the complete text."
                    ),
                    "default": "full",
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
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="search_and_read",
        description=(
            "Combined: search for papers, then immediately fetch the FULL TEXT of "
            "the best match. Returns the complete article, which can be 50,000+ "
            "characters for a typical journal article.\n\n"
            "Best for: short papers, or when you need a complete overview.\n"
            "For targeted questions about long papers, prefer: search_papers → "
            "fetch_fulltext(mode='sections') → fetch_fulltext(mode='section') or "
            "search_in_article. This avoids loading the entire text into context.\n\n"
            "USAGE NOTE — Query Expansion: Decompose natural language questions into "
            "2-6 technical keywords before calling. VERBATIM queries are discouraged. "
            "Example: 'What are the latest advances in CRISPR gene editing?' → "
            "'CRISPR Cas9 gene editing recent advances'."
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
        name="search_in_article",
        description=(
            "Search within a cached article for specific terms or concepts. "
            "Returns a distribution heatmap showing WHERE each term concentrates "
            "across the article, plus BM25-ranked text snippets with surrounding "
            "context and section attribution.\n\n"
            "This is often the fastest way to answer a specific question about a "
            "paper — more efficient than reading the full text or even a whole "
            "section. Works even when section detection is poor.\n\n"
            "TIPS:\n"
            "- Use 2-5 varied terms: synonyms, abbreviations, different word forms "
            "  (e.g. 'algorithmic bias', 'discrimination', 'fairness', 'GDPR').\n"
            "- Multi-word phrases work: 'due diligence', 'surveillance capitalism'.\n"
            "- Follow up with fetch_fulltext(mode='range') using the character "
            "  offsets from results to read broader context around a match.\n\n"
            "The article must have been fetched previously via fetch_fulltext or "
            "search_and_read."
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
        elif name == "search_in_article":
            return await _handle_search_in_article(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def _handle_search(args: dict) -> list[TextContent]:
    query = args["query"]
    limit = min(args.get("limit", 5), 20)
    source = args.get("source", "all")
    start_year = args.get("start_year")
    end_year = args.get("end_year")
    venue = args.get("venue")

    results = []  # unified list of normalized result dicts
    seen_dois = set()

    # ── Helper to normalize a result ─────────────────────────────────
    def _authors_str(authors: list) -> str:
        if not authors:
            return "Unknown"
        names = authors[:3]
        s = ", ".join(names)
        if len(authors) > 3:
            s += f" +{len(authors)-3} more"
        return s

    # ── 1. Zotero (always first, unless source excludes it) ──────────
    if source in ("all", "zotero"):
        try:
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
                if doi:
                    seen_dois.add(zotero._normalize_doi(doi))
                results.append({
                    "title": item.get("title") or "Untitled",
                    "authors": author_names,
                    "year": (item.get("date") or "")[:4] or None,
                    "doi": doi or None,
                    "abstract": (item.get("abstractNote") or "").strip() or None,
                    "citations": None,
                    "venue": item.get("publicationTitle") or None,
                    "found_in": ["zotero"],
                    "in_zotero": True,
                    "has_oa_pdf": True,
                    "s2_id": None,
                })
        except Exception:
            logger.exception("Zotero search failed")

    # Pre-fetch DOI index once (used to check Zotero membership below)
    zot_index = await zotero.get_doi_index()

    # ── 2. Semantic Scholar ──────────────────────────────────────────
    if source in ("all", "semantic_scholar"):
        try:
            s2 = await apis.s2_search(
                query, limit=limit,
                start_year=start_year, end_year=end_year,
            )
            for paper in s2.get("data", []):
                doi = apis.extract_doi(paper)
                doi_norm = zotero._normalize_doi(doi) if doi else None
                # Deduplicate
                if doi_norm and doi_norm in seen_dois:
                    # Enrich existing result
                    for r in results:
                        if r.get("doi") and zotero._normalize_doi(r["doi"]) == doi_norm:
                            if "semantic_scholar" not in r["found_in"]:
                                r["found_in"].append("semantic_scholar")
                            r["citations"] = r["citations"] or paper.get("citationCount")
                            r["s2_id"] = paper.get("paperId")
                            if not r["abstract"] and paper.get("abstract"):
                                r["abstract"] = paper["abstract"]
                            break
                    continue
                if doi_norm:
                    seen_dois.add(doi_norm)
                in_zot = doi_norm in zot_index if doi_norm else False
                results.append({
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
        except Exception as e:
            logger.warning("Semantic Scholar search failed: %s", e)

    # ── 3. OpenAlex ──────────────────────────────────────────────────
    if source in ("all", "openalex"):
        try:
            oa = await apis.openalex_search(
                query, limit=limit,
                start_year=start_year, end_year=end_year, venue=venue,
            )
            for work in oa.get("results", []):
                doi = apis.extract_doi(work)
                doi_norm = zotero._normalize_doi(doi) if doi else None
                if doi_norm and doi_norm in seen_dois:
                    for r in results:
                        if r.get("doi") and zotero._normalize_doi(r["doi"]) == doi_norm:
                            if "openalex" not in r["found_in"]:
                                r["found_in"].append("openalex")
                            r["citations"] = r["citations"] or work.get("cited_by_count")
                            if not r["abstract"]:
                                r["abstract"] = _reconstruct_abstract(work.get("abstract_inverted_index"))
                            break
                    continue
                if doi_norm:
                    seen_dois.add(doi_norm)
                authors = []
                for auth in (work.get("authorships") or []):
                    if not auth:
                        continue
                    name = (auth.get("author") or {}).get("display_name")
                    if name:
                        authors.append(name)
                    if len(authors) >= 5:
                        break
                in_zot = doi_norm in zot_index if doi_norm else False
                results.append({
                    "title": work.get("title") or "Untitled",
                    "authors": authors,
                    "year": work.get("publication_year"),
                    "doi": doi,
                    "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")) or None,
                    "citations": work.get("cited_by_count"),
                    "venue": ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or None,
                    "found_in": ["openalex"],
                    "in_zotero": in_zot,
                    "has_oa_pdf": (work.get("open_access") or {}).get("is_oa", False),
                    "s2_id": None,
                })
        except Exception as e:
            logger.warning("OpenAlex search failed: %s", e)

    # ── 4. Ex Libris Primo ───────────────────────────────────────────
    if source in ("all", "primo"):
        try:
            primo_results = await apis.primo_search(
                query, limit=limit,
                start_year=start_year, end_year=end_year,
            )
            for r in primo_results:
                doi = (r.get("doi") or "").strip()
                doi_norm = zotero._normalize_doi(doi) if doi else None
                if doi_norm and doi_norm in seen_dois:
                    for existing in results:
                        if existing.get("doi") and zotero._normalize_doi(existing["doi"]) == doi_norm:
                            if "primo" not in existing["found_in"]:
                                existing["found_in"].append("primo")
                            if not existing.get("_primo_proxy_url"):
                                existing["_primo_proxy_url"] = r.get("_primo_proxy_url")
                            if not existing.get("_primo_oa_url"):
                                existing["_primo_oa_url"] = r.get("_primo_oa_url")
                                existing["has_oa_pdf"] = existing["has_oa_pdf"] or r["has_oa_pdf"]
                            break
                    continue
                if doi_norm:
                    seen_dois.add(doi_norm)
                in_zot = doi_norm in zot_index if doi_norm else False
                r["in_zotero"] = in_zot
                results.append(r)
        except Exception:
            logger.exception("Primo search failed")

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

    # ── 5. Semantic re-ranking ─────────────────────────────────────
    #
    # Use sentence-transformers (all-MiniLM-L6-v2) to compute cosine
    # similarity between the query and each result's abstract/title.
    # Zotero items still get a priority boost, but within each tier
    # results are ordered by true semantic relevance.
    # Falls back to composite scoring if the model is unavailable.
    results = await rerank_results(query, results)

    # ── 6. Format output as RAG-friendly text ────────────────────────
    if not results:
        return [TextContent(type="text", text=f"No papers found for '{query}'.")]

    text = f"Found {len(results)} papers for '{query}':\n"
    text += "=" * 60 + "\n\n"

    for i, r in enumerate(results):
        # Header line with index, title, and availability badges
        badges = []
        if r["in_zotero"]:
            badges.append("★ IN ZOTERO")
        if r["has_oa_pdf"]:
            badges.append("OA")
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
            text += f"    Venue: {r['venue']}\n"
        if r.get("doi"):
            text += f"    DOI: {r['doi']}\n"
        text += f"    Sources: {', '.join(r['found_in'])}\n"
        if r.get("_semantic_similarity") is not None:
            text += f"    Relevance: {r['_semantic_similarity']:.3f}\n"

        # Abstract / Preview
        abstract = r.get("abstract") or ""
        if abstract:
            # Truncate long abstracts for the listing
            if len(abstract) > 400:
                abstract = abstract[:400] + "..."
            text += f"\n    {abstract}\n"

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
        else:
            text += "No DOI — full text retrieval not available for this result."
        text += "\n\n"

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


async def _handle_fetch_pdf(args: dict) -> list[TextContent]:
    doi = args["doi"]
    use_proxy = args.get("use_proxy", False)
    pages_str = args.get("pages")
    mode = args.get("mode", "full")
    section_name = args.get("section")
    range_start = args.get("range_start")
    range_end = args.get("range_end")

    # ── Cache read: fastest path — skip all network/PDF work ────────
    cached_article = text_cache.get_cached(doi)
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
            new_sections = content_extractor.detect_sections_from_text(cached_article.text)
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
                )
        return _apply_mode_filter(
            cached_article, mode, section_name, range_start, range_end
        )

    # ── Step 0: Check Zotero FIRST ──────────────────────────────────
    zot_result = await zotero.get_paper_from_zotero(doi)
    if zot_result and zot_result.get("found"):
        # Got fulltext directly (already extracted by Zotero — best case!)
        if zot_result.get("text"):
            raw_text = zot_result["text"]
            sections = content_extractor.detect_sections_from_text(raw_text)
            cached_article = text_cache.put_cached(
                doi, raw_text, zot_result["source"],
                sections=sections,
                section_detection="text_heuristic",
                word_count=len(raw_text.split()),
            )
            if mode != "full":
                return _apply_mode_filter(
                    cached_article, mode, section_name, range_start, range_end
                )
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
            return [TextContent(type="text", text=text)]

        # Got PDF path from Zotero — extract text from disk
        if zot_result.get("pdf_path"):
            return _cache_pdf_and_return(
                zot_result["pdf_path"], doi, zot_result["source"],
                pages_str, mode, section_name, range_start, range_end,
            )

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
    return [TextContent(
        type="text",
        text=(
            f"Failed to fetch PDF for DOI: {doi}\n"
            f"Tried sources: {', '.join(sources_tried) or 'none found'}\n"
            f"Proxy used: {use_proxy}\n"
            "The paper may require institutional access or the PDF links may be broken."
        ),
    )]


async def _handle_search_and_read(args: dict) -> list[TextContent]:
    query = args["query"]
    result_index = args.get("result_index", 0)
    use_proxy = args.get("use_proxy", False)

    # Search first
    try:
        s2 = await apis.s2_search(query, limit=5)
        papers = s2.get("data", [])
    except Exception as e:
        return [TextContent(type="text", text=f"Search failed: {e}")]

    if not papers:
        return [TextContent(type="text", text=f"No papers found for '{query}'")]

    if result_index >= len(papers):
        return [TextContent(
            type="text",
            text=f"Result index {result_index} out of range (found {len(papers)} results)",
        )]

    paper = papers[result_index]
    doi = apis.extract_doi(paper)

    text = f"Selected paper [{result_index}]: {paper.get('title')}\n"
    text += f"DOI: {doi}\n\n"

    if not doi:
        text += "No DOI found for this paper. Cannot fetch PDF.\n"
        if paper.get("abstract"):
            text += f"\nAbstract:\n{paper['abstract']}\n"
        return [TextContent(type="text", text=text)]

    # Fetch and extract
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

        return [TextContent(type="text", text=text)]

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
        return [TextContent(type="text", text=full)]

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
        return [TextContent(type="text", text=text)]

    if mode == "range":
        start = range_start or 0
        end = range_end or min(start + config.max_context_length, len(cached.text))
        snippet = cached.text[start:end]
        header = (
            f"Character range [{start}:{end}] for DOI: {doi}\n"
            f"Source: {cached.source}\n"
            + "=" * 60 + "\n\n"
        )
        return [TextContent(type="text", text=header + snippet)]

    # mode == "full" (default)
    header = (
        f"Full text (cached) for DOI: {doi}\n"
        f"Source: {cached.source}\n"
        + "=" * 60 + "\n\n"
    )
    full = header + cached.text
    if len(full) > config.max_context_length:
        full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return [TextContent(type="text", text=full)]


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
    if pages_str:
        return _format_extracted_pdf(pdf_source, doi, source, pages_str)

    result = pdf_extractor.extract_text_with_sections(pdf_source)
    raw_text = result["text"]
    cached_article = text_cache.put_cached(
        doi, raw_text, source,
        sections=result["sections"],
        section_detection=result.get("section_detection", "pdf_font_analysis"),
        word_count=len(raw_text.split()),
    )

    if mode != "full":
        return _apply_mode_filter(cached_article, mode, section_name, range_start, range_end)

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
    full_text = header + raw_text
    if len(full_text) > config.max_context_length:
        full_text = full_text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    return [TextContent(type="text", text=full_text)]


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

