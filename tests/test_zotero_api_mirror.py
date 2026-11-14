"""The server's Zotero copy built from the Web API (replaces the desktop push)."""

import json
import sqlite3

import httpx
import pytest

from academic_mcp import zotero_api_mirror as mirror
from academic_mcp import zotero_sqlite
from academic_mcp.zotero import zot_config


@pytest.mark.parametrize("value,parsed,expected", [
    ("2022", "2022", "2022-00-00 2022"),
    ("March 2012", "2012-03", "2012-03-00 March 2012"),
    ("2025-03-28", "2025-03-28", "2025-03-28 2025-03-28"),
    ("Spring 1999", "", "1999-00-00 Spring 1999"),
    ("forthcoming", "", "0000-00-00 forthcoming"),
])
def test_dates_are_stored_as_zotero_does(value, parsed, expected):
    assert mirror.multipart_date(value, parsed) == expected


def test_day_first_locale_matches_the_desktop(monkeypatch):
    monkeypatch.setenv("ZOTERO_DATE_ORDER", "dmy")
    assert mirror.multipart_date("06/12/2019", "2019-06-12") == "2019-12-06 06/12/2019"
    monkeypatch.setenv("ZOTERO_DATE_ORDER", "mdy")
    assert mirror.multipart_date("06/12/2019", "2019-06-12") == "2019-06-12 06/12/2019"


def test_timestamps_are_stored_as_zotero_does():
    assert mirror.sql_datetime("2025-04-15T13:32:25Z") == "2025-04-15 13:32:25"


def _item(key, version, **data):
    base = {"key": key, "version": version, "itemType": "journalArticle", "title": "",
            "creators": [], "tags": [], "collections": [], "relations": {},
            "dateAdded": "2026-01-01T10:00:00Z", "dateModified": "2026-01-02T11:00:00Z"}
    base.update(data)
    meta = {"parsedDate": data.get("date", "")[:4]} if data.get("date") else {}
    return {"key": key, "version": version, "meta": meta, "data": base}


class FakeZotero:
    """Just enough of the Zotero Web API, with versions."""

    def __init__(self):
        self.version = 10
        self.items = {}
        self.collections = {}
        self.deleted = {}      # key -> version deleted at
        self.fulltext = {}     # attachment key -> (version, content)

    def put(self, item):
        self.version += 1
        item["version"] = item["data"]["version"] = self.version
        self.items[item["key"]] = item

    def delete(self, key):
        self.version += 1
        self.items.pop(key)
        self.deleted[key] = self.version

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = dict(request.url.params)
        since = int(q.get("since", 0))
        headers = {"Last-Modified-Version": str(self.version)}
        if path == "/schema":
            return httpx.Response(200, json={"itemTypes": [
                {"itemType": "journalArticle", "fields": [{"field": "title"}, {"field": "DOI"}, {"field": "date"},
                                                          {"field": "url"}, {"field": "extra"}],
                 "creatorTypes": [{"creatorType": "author"}]},
            ]})
        if path == "/users/1/groups":
            return httpx.Response(200, json=[])
        if path == "/users/1/collections":
            return httpx.Response(200, json={}, headers=headers)
        if path == "/users/1/items" and q.get("format") == "versions":
            return httpx.Response(200, json={k: i["version"] for k, i in self.items.items() if i["version"] > since},
                                  headers=headers)
        if path == "/users/1/items":
            keys = q["itemKey"].split(",")
            return httpx.Response(200, json=[self.items[k] for k in keys if k in self.items], headers=headers)
        if path == "/users/1/deleted":
            return httpx.Response(200, json={"items": [k for k, v in self.deleted.items() if v > since],
                                             "collections": []}, headers=headers)
        if path == "/users/1/fulltext":
            return httpx.Response(200, json={k: v for k, (v, _) in self.fulltext.items() if v > since},
                                  headers=headers)
        if path.startswith("/users/1/items/") and path.endswith("/fulltext"):
            key = path.split("/")[4]
            version, content = self.fulltext[key]
            return httpx.Response(200, json={"content": content, "indexedPages": 3, "totalPages": 3})
        return httpx.Response(404, json={})


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = FakeZotero()
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mirror.httpx, "AsyncClient", client)
    monkeypatch.setenv("ZOTERO_API_MIRROR", "true")
    monkeypatch.delenv("ZOTERO_API_MIRROR_SEED", raising=False)
    monkeypatch.setattr(zot_config, "api_key", "k")
    monkeypatch.setattr(zot_config, "user_id", "1")
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "db_path", str(tmp_path / "mirror" / "zotero.sqlite"))
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "storage_path", str(tmp_path / "mirror" / "storage"))
    monkeypatch.setattr(zotero_sqlite, "_maybe_schedule_shadow_refresh", lambda *a: None)
    monkeypatch.setenv("ZOTERO_CATALOG_ENABLED", "false")
    zotero_sqlite._field_ids.clear()
    yield fake
    zotero_sqlite._field_ids.clear()


def _library(fake):
    fake.put(_item("PARENT01", 0, title="Making Standards Stick", DOI="10.1177/0170840612443623",
                   date="2012", creators=[{"creatorType": "author", "firstName": "Kristin", "lastName": "Sandholtz"}],
                   tags=[{"tag": "compliance"}]))
    fake.put(_item("ATTACH01", 0, itemType="attachment", parentItem="PARENT01", linkMode="imported_file",
                   contentType="application/pdf", filename="Sandholtz 2012.pdf", md5="abc", mtime=1700000000000,
                   title="Full Text PDF"))
    fake.put(_item("TRASHED1", 0, title="Old draft", deleted=1))
    fake.fulltext["ATTACH01"] = (fake.version, "Making Standards Stick. Full text here.")


async def test_build_from_api_reads_like_the_desktop_database(api):
    _library(api)
    summary = await mirror.sync()
    assert not summary["errors"]

    item = await zotero_sqlite.search_by_key("PARENT01")
    assert item.title == "Making Standards Stick" and item.DOI == "10.1177/0170840612443623"
    assert item.date == "2012-00-00 2012" and item.creators[0].lastName == "Sandholtz"
    assert item.tags == ["compliance"] and item.dateModified == "2026-01-02 11:00:00"
    assert (await zotero_sqlite.search_by_doi("10.1177/0170840612443623")).key == "PARENT01"

    content = await zotero_sqlite.get_paper_content(item)
    assert content.attachment_key == "ATTACH01"
    ft = zotero_sqlite.Path(zotero_sqlite.sqlite_config.storage_path) / "ATTACH01" / ".zotero-ft-cache"
    assert ft.read_text() == "Making Standards Stick. Full text here."

    listed = {i["item_key"] for i in await zotero_sqlite.list_items_for_semantic_index()}
    assert listed == {"PARENT01"}  # trashed items and attachments are not indexed

    st = mirror.status()
    assert st["built"] and st["libraries"][0]["items"] == 3


async def test_incremental_sync_applies_changes_and_deletions(api):
    _library(api)
    await mirror.sync()
    api.put(_item("PARENT01", 0, title="Making Standards Stick: A Theory", DOI="10.1177/0170840612443623"))
    api.delete("TRASHED1")
    summary = await mirror.sync(reconcile=False)
    lib = summary["libraries"]["/users/1"]
    assert lib["items"] == 1 and lib["deleted"] == 1
    assert (await zotero_sqlite.search_by_key("PARENT01")).title == "Making Standards Stick: A Theory"
    assert await zotero_sqlite.search_by_key("TRASHED1") is None
    # Unchanged since the last sync: nothing is fetched.
    again = await mirror.sync(reconcile=False)
    assert not any(v for k, v in again["libraries"]["/users/1"].items() if k != "reconciled")


async def test_reconciliation_repairs_a_diverged_copy(api, tmp_path):
    _library(api)
    await mirror.sync()
    db = sqlite3.connect(zotero_sqlite.sqlite_config.db_path)
    # Simulate drift: a stray local item and a stale version of a real one.
    db.execute("INSERT INTO items(itemTypeID, libraryID, key, version) VALUES(1, 1, 'STRAY001', 1)")
    db.execute("UPDATE items SET version=1 WHERE key='PARENT01'")
    db.commit()
    db.close()
    summary = await mirror.sync(reconcile=True)
    lib = summary["libraries"]["/users/1"]
    assert lib["deleted"] == 1 and lib["items"] == 1
    assert await zotero_sqlite.search_by_key("STRAY001") is None


async def test_seeded_build_keeps_unchanged_rows(api, tmp_path, monkeypatch):
    _library(api)
    await mirror.sync()
    seed = tmp_path / "seed.sqlite"
    src = sqlite3.connect(zotero_sqlite.sqlite_config.db_path)
    dst = sqlite3.connect(seed)
    src.backup(dst)
    src.close()
    dst.close()
    before = sqlite3.connect(seed).execute("SELECT itemID, key FROM items ORDER BY itemID").fetchall()

    monkeypatch.setattr(zotero_sqlite.sqlite_config, "db_path", str(tmp_path / "fresh" / "zotero.sqlite"))
    monkeypatch.setenv("ZOTERO_API_MIRROR_SEED", str(seed))
    summary = await mirror.sync()
    lib = summary["libraries"]["/users/1"]
    assert lib["items"] == 0  # every version already matched
    after = sqlite3.connect(zotero_sqlite.sqlite_config.db_path).execute(
        "SELECT itemID, key FROM items ORDER BY itemID").fetchall()
    assert after == before


async def test_api_mirror_needs_no_shadow_copy(api):
    _library(api)
    await mirror.sync()
    assert mirror.is_api_mirror(zotero_sqlite.sqlite_config.db_path)
    called = []
    zotero_sqlite._maybe_schedule_shadow_refresh = lambda *a: called.append(a)
    conn = await zotero_sqlite._get_connection()
    await conn.close()
    assert not called


async def test_seeded_full_text_is_adopted_not_rewritten(api, tmp_path, monkeypatch):
    _library(api)
    await mirror.sync()
    ft = zotero_sqlite.Path(zotero_sqlite.sqlite_config.storage_path) / "ATTACH01" / ".zotero-ft-cache"
    stat = ft.stat()
    # The desktop recorded a different version for text the server has not changed since.
    db = sqlite3.connect(zotero_sqlite.sqlite_config.db_path)
    db.execute("UPDATE fulltextItems SET version=1")
    db.commit()
    db.close()
    summary = await mirror.sync(reconcile=True)
    assert summary["libraries"]["/users/1"]["fulltext"] == 0
    assert ft.stat().st_ino == stat.st_ino and ft.stat().st_mtime_ns == stat.st_mtime_ns
    # Text changed on the server after that is downloaded.
    api.version += 1
    api.fulltext["ATTACH01"] = (api.version, "Revised text.")
    summary = await mirror.sync(reconcile=False)
    assert summary["libraries"]["/users/1"]["fulltext"] == 1 and ft.read_text() == "Revised text."
