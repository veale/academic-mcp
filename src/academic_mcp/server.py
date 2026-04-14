"""MCP Server — tool definitions and request handlers."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

try:
    from . import apis, pdf_fetcher, pdf_extractor, zotero, zotero_sqlite
    from .config import config
    from .reranker import rerank_results
except ImportError:
    from academic_mcp import apis, pdf_fetcher, pdf_extractor, zotero, zotero_sqlite
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
            "Use author:LastName to search by author."
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
                    "enum": ["all", "semantic_scholar", "openalex", "zotero"],
                    "description": (
                        "Which sources to search. 'all' (default) searches Zotero + "
                        "Semantic Scholar + OpenAlex and deduplicates. 'zotero' searches "
                        "only your library."
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
            "Get the full text of a paper for analysis. This is the tool to call "
            "when search_papers returns a result you want to read in full. "
            "Checks Zotero first (pre-extracted text if available, then PDF), "
            "then tries Unpaywall, Semantic Scholar, OpenAlex, stealth browser, "
            "and optionally institutional proxy."
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
                    "description": "Optional page range, e.g. '1-5'. Default: all.",
                },
            },
            "required": ["doi"],
        },
    ),
    Tool(
        name="search_and_read",
        description=(
            "Combined: search for papers, then immediately fetch full text of "
            "the best match. Use this when you know what paper you want and "
            "want its content in one step.\n\n"
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
            "Useful for debugging access issues."
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

    # ── 4. For Zotero items without abstracts, try getting a preview ─
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
                text += f"Full text available. Call fetch_fulltext(doi=\"{r['doi']}\") to read."
            elif r["has_oa_pdf"]:
                text += f"Open access PDF available. Call fetch_fulltext(doi=\"{r['doi']}\") to read."
            else:
                text += f"May need proxy. Call fetch_fulltext(doi=\"{r['doi']}\", use_proxy=true) to try."
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

    # ── Step 0: Check Zotero FIRST ──────────────────────────────────
    zot_result = await zotero.get_paper_from_zotero(doi)
    if zot_result and zot_result.get("found"):
        # Got fulltext directly (already extracted by Zotero — best case!)
        if zot_result.get("text"):
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
            text = header + zot_result["text"]
            if len(text) > config.max_context_length:
                text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
            return [TextContent(type="text", text=text)]

        # Got PDF path from Zotero — extract text from disk
        if zot_result.get("pdf_path"):
            return _format_extracted_pdf(
                zot_result["pdf_path"], doi, zot_result["source"], pages_str
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

    if not candidate_urls and not use_proxy:
        return [TextContent(
            type="text",
            text=(
                f"No open access PDF URLs found for DOI: {doi}\n"
                "Try with use_proxy=true if you have institutional access configured."
            ),
        )]

    # Fetch the PDF (streams to disk, returns Path)
    pdf_path, source = await pdf_fetcher.fetch_pdf(
        candidate_urls, doi=doi, use_proxy=use_proxy
    )

    if not pdf_path:
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

    # Extract text (reads from disk — near-zero RAM)
    if pages_str:
        parts = pages_str.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
        extracted_text = pdf_extractor.extract_text_by_pages(pdf_path, start, end)
        return [TextContent(
            type="text",
            text=(
                f"Extracted text from pages {pages_str} of DOI: {doi}\n"
                f"Source: {source}\n\n"
                f"{extracted_text}"
            ),
        )]

    result = pdf_extractor.extract_text(pdf_path)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

