"""Core business-logic layer for academic-mcp.

Each module exposes pure async functions that take typed parameters and
return Pydantic models (or basic Python types where Pydantic adds no value).
These functions are called by the MCP handler layer (server.py) and will
also be called by the FastAPI webapp layer (Phase 1+).

Modules:
  libraries   – list_libraries, refresh_zotero_index
  search      – search_zotero, search_by_doi, search_papers
  semantic    – semantic_search_zotero
  paper       – get_paper
  citations   – get_citations, get_references, get_citation_tree
  in_article  – search_in_article
  fetch       – fetch_article (the full fetch/extract pipeline)
  types       – shared Pydantic return types
"""
