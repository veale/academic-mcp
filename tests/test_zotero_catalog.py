import httpx
import pytest

from academic_mcp import zotero_catalog as catalog
from academic_mcp.zotero import zot_config


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CATALOG_ENABLED", "true")
    monkeypatch.setattr(catalog.config, "pdf_cache_dir", tmp_path / "pdfs")
    monkeypatch.setattr(zot_config, "api_key", "test-key")
    monkeypatch.setattr(zot_config, "user_id", "1")
    monkeypatch.setattr(zot_config, "library_type", "user")
    monkeypatch.setattr(catalog, "_mirror_libraries", lambda: ({"/users/1": 10}, {}))
    return tmp_path


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        catalog.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )


async def test_changes_deletions_and_attachments_overlay_snapshot(backend, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        headers = {"Last-Modified-Version": "12"}
        if request.url.path.endswith("/deleted"):
            return httpx.Response(200, headers=headers, json={"items": ["D"]})
        assert request.url.params["since"] == "10"
        return httpx.Response(
            200,
            headers=headers,
            json=[
                {"key": "A", "data": {"key": "A", "deleted": 1}},
                {
                    "key": "B",
                    "data": {
                        "key": "B",
                        "itemType": "journalArticle",
                        "title": "Updated",
                    },
                },
                {
                    "key": "C",
                    "data": {
                        "key": "C",
                        "itemType": "journalArticle",
                        "title": "New paper",
                    },
                },
                {
                    "key": "ATT",
                    "data": {
                        "key": "ATT",
                        "parentItem": "C",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                    },
                },
            ],
        )

    transport(monkeypatch, handler)
    base = [{"item_key": k, "title": "Old " + k} for k in ["A", "B", "D"]]
    result = {x["item_key"]: x for x in await catalog.overlay(base)}
    assert set(result) == {"B", "C"}
    assert result["B"]["title"] == "Updated"
    assert result["C"]["attachment_key"] == "ATT"
    count = len(calls)
    await catalog.overlay(base)
    assert len(calls) == count
    assert catalog.status()["libraries"][0]["version"] == 12


async def test_failed_page_cannot_advance_version_or_publish_partial_data(
    backend, monkeypatch
):
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/deleted"):
            return httpx.Response(403)
        return httpx.Response(
            200,
            headers={"Last-Modified-Version": "11"},
            json=[
                {
                    "key": "NEW",
                    "data": {"key": "NEW", "itemType": "book", "title": "Incomplete"},
                }
            ],
        )

    transport(monkeypatch, handler)
    with pytest.raises(catalog.CatalogUnavailable):
        await catalog.overlay([])
    with catalog._connect() as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM libraries").fetchone()[0] == 0


async def test_catalog_paginates_before_checkpointing(backend, monkeypatch):
    pages = []

    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        headers = {"Last-Modified-Version": "11"}
        if request.url.path.endswith("/deleted"):
            return httpx.Response(200, headers=headers, json={"items": []})
        start = int(request.url.params["start"])
        pages.append(start)
        items = [
            {
                "key": str(i),
                "data": {"key": str(i), "itemType": "book", "title": "Book"},
            }
            for i in range(start, min(start + 100, 101))
        ]
        return httpx.Response(200, headers=headers, json=items)

    transport(monkeypatch, handler)
    assert len(await catalog.overlay([])) == 101
    assert pages == [0, 100]


async def test_changing_library_versions_do_not_publish_partial_snapshot(
    backend, monkeypatch
):
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/deleted"):
            return httpx.Response(
                200, headers={"Last-Modified-Version": "12"}, json={"items": []}
            )
        return httpx.Response(200, headers={"Last-Modified-Version": "11"}, json=[])

    transport(monkeypatch, handler)
    with pytest.raises(catalog.CatalogUnavailable, match="kept changing"):
        await catalog.overlay([])
    assert not catalog.status()["libraries"]


async def test_newly_synced_doi_refreshes_a_previously_cached_lookup(
    backend, monkeypatch
):
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        headers = {"Last-Modified-Version": "11"}
        if request.url.path.endswith("/deleted"):
            return httpx.Response(200, headers=headers, json={"items": []})
        return httpx.Response(
            200,
            headers=headers,
            json=[
                {
                    "key": "NEW",
                    "data": {
                        "key": "NEW",
                        "itemType": "journalArticle",
                        "title": "New paper",
                        "DOI": "10.1234/NEW",
                    },
                },
                {
                    "key": "PDF",
                    "data": {
                        "key": "PDF",
                        "itemType": "attachment",
                        "parentItem": "NEW",
                        "contentType": "application/pdf",
                    },
                },
            ],
        )

    transport(monkeypatch, handler)
    result = await catalog.update_doi_index(
        {"10.1234/existing": {"item_key": "OLD", "attachment_key": None}}
    )
    assert result["10.1234/new"]["item_key"] == "NEW"
    assert result["10.1234/new"]["attachment_key"] == "PDF"
    assert result["10.1234/existing"]["item_key"] == "OLD"


async def test_group_item_lookup_uses_its_actual_library(backend, monkeypatch):
    from academic_mcp import zotero
    from unittest.mock import AsyncMock

    monkeypatch.setattr(zotero, "_local_api_get", AsyncMock(return_value=None))
    monkeypatch.setattr(zotero, "_web_api_get", AsyncMock(return_value=None))
    monkeypatch.setattr(catalog, "library_for_key", lambda key: "/groups/123")
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"key": "GROUPKEY", "data": {"title": "Group paper"}}
        )

    transport(monkeypatch, handler)
    item = await zotero._get_item("GROUPKEY")
    assert item["data"]["title"] == "Group paper"
    assert paths == ["/groups/123/items/GROUPKEY"]


async def test_new_catalog_records_hydrate_search_hits_without_snapshot_rows(
    backend, monkeypatch
):
    def handler(request):
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        headers = {"Last-Modified-Version": "11"}
        if request.url.path.endswith("/deleted"):
            return httpx.Response(200, headers=headers, json={"items": []})
        return httpx.Response(
            200,
            headers=headers,
            json=[
                {
                    "key": "NEW",
                    "data": {
                        "key": "NEW",
                        "itemType": "journalArticle",
                        "title": "Fresh paper",
                        "DOI": "10.1234/new",
                        "creators": [
                            {
                                "firstName": "Jane",
                                "lastName": "Researcher",
                                "creatorType": "author",
                            }
                        ],
                    },
                }
            ],
        )

    transport(monkeypatch, handler)
    await catalog.refresh()
    results = await catalog.hydrate_keys(["NEW"], {})
    assert results["NEW"].title == "Fresh paper"
    assert results["NEW"].creators[0].display_name == "Jane Researcher"
    assert results["NEW"].DOI == "10.1234/new"
