# Webapp API

All endpoints mount under `/webapp`.  Auth endpoints and `/api/health` are open; everything else requires a valid session cookie (set by `POST /api/auth/login`).

Interactive docs: `/webapp/api/docs`

## Auth

| Method | Path | Body / Params | Response |
|--------|------|---------------|----------|
| GET | `/api/health` | — | `{ok: true}` |
| POST | `/api/auth/login` | `{password}` | `{ok: true}` + cookie |
| POST | `/api/auth/logout` | — | `{ok: true}` |

## Search

| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/api/search` | `q`, `limit`, `zotero_only`, `semantic`, `include_scite`, `domain_hint` | Unified search pipeline |
| GET | `/api/semantic` | `q`, `k`, `library_id` | Semantic Zotero search |
| GET | `/api/paper` | `doi` | Single-paper metadata |

## Article

| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/api/article` | `doi` \| `zotero_key` \| `url` | Fetch article; returns `cache_key` + viewer availability |
| GET | `/api/article/text` | `cache_key` | Extracted text + section index |
| GET | `/api/article/html` | `cache_key` | Cleaned HTML (404 when unavailable) |
| GET | `/api/article/pdf` | `cache_key` | PDF bytes; Range supported |
| GET | `/api/article/highlights` | `cache_key`, `q`, `k` | BM25 passages + PDF page rects |
| GET | `/api/article/in_article` | `cache_key`, `q` | BM25 search with dispersion heatmap |

**Viewer selection rule:** call `/api/article` first; the `viewers` field tells you which of `{pdf, html, text}` are available for that `cache_key`.

## Citations

| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/api/citations` | `doi`, `direction` (`in`\|`out`\|`tree`), `limit` | Forward / backward / both |
| GET | `/api/citations/search` | `doi`, `q`, `direction` (`in`\|`out`), `limit` | Keyword-filtered citations |

## Zotero

| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/api/zotero/libraries` | — | All Zotero libraries |
| GET | `/api/zotero/search` | `q`, `limit` | Lexical Zotero search |
| GET | `/api/zotero/deeplink` | `zotero_key` | `{select_url, open_pdf_url}` for desktop hand-off |
