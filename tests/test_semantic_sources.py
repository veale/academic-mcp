import sqlite3
from academic_mcp import zotero_sqlite, text_cache


async def test_trashed_items_and_attachments_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CATALOG_ENABLED", "false")
    path = tmp_path / "zotero.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE libraries(libraryID INTEGER);INSERT INTO libraries VALUES(1);
            CREATE TABLE fields(fieldID INTEGER,fieldName TEXT);INSERT INTO fields VALUES(1,'title');
            CREATE TABLE itemTypes(itemTypeID INTEGER,typeName TEXT);INSERT INTO itemTypes VALUES(1,'journalArticle'),(2,'attachment');
            CREATE TABLE items(itemID INTEGER, key TEXT, dateModified TEXT,itemTypeID INTEGER,libraryID INTEGER);
            INSERT INTO items VALUES(1,'ACTIVE','2026-01-01',1,1),(2,'TRASH','2026-01-01',1,1),(3,'PDFTRASH','2026-01-01',2,1),(4,'PDFLIVE','2026-01-01',2,1);
            CREATE TABLE deletedItems(itemID INTEGER);INSERT INTO deletedItems VALUES(2),(3);
            CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER);INSERT INTO itemData VALUES(1,1,1),(2,1,2);
            CREATE TABLE itemDataValues(valueID INTEGER,value TEXT);INSERT INTO itemDataValues VALUES(1,'Active paper'),(2,'Trashed paper');
            CREATE TABLE itemAttachments(itemID INTEGER,parentItemID INTEGER,contentType TEXT);
            INSERT INTO itemAttachments VALUES(3,1,'application/pdf'),(4,1,'application/pdf');
            CREATE TABLE creators(creatorID INTEGER,firstName TEXT,lastName TEXT);
            CREATE TABLE itemCreators(itemID INTEGER,creatorID INTEGER,orderIndex INTEGER);
            CREATE TABLE itemTags(itemID INTEGER,tagID INTEGER);CREATE TABLE tags(tagID INTEGER,name TEXT);
        """)
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "db_path", str(path))
    monkeypatch.setattr(
        zotero_sqlite, "_maybe_schedule_shadow_refresh", lambda *args: None
    )
    for listing in [
        zotero_sqlite.list_items_for_semantic_index,
        zotero_sqlite.list_items_for_lexical_index,
    ]:
        items = await listing()
        assert [i["item_key"] for i in items] == ["ACTIVE"]
        assert items[0]["attachment_key"] == "PDFLIVE"


def test_cache_reads_preserve_content_modification_time(tmp_path, monkeypatch):
    monkeypatch.setattr(text_cache.config, "pdf_cache_dir", tmp_path)
    text_cache.put_cached("10.1234/test", "Text", "web", [])
    path = text_cache._cache_path("10.1234/test")
    before = path.stat().st_mtime_ns
    text_cache.get_cached("10.1234/test")
    assert path.stat().st_mtime_ns == before
