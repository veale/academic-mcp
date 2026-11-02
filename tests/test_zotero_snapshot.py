"""An atomically published server mirror needs no second shadow copy."""
import sqlite3
from unittest.mock import Mock

import pytest
from academic_mcp import zotero_sqlite


@pytest.mark.parametrize('snapshot', [True, False])
async def test_shadow_refresh_only_for_live_databases(tmp_path, monkeypatch, snapshot):
    path = tmp_path / 'zotero.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE libraries (libraryID INTEGER)')
        db.execute('INSERT INTO libraries VALUES (1)')
    if snapshot:
        (tmp_path / '.last-sync').write_text('2026-11-01T13:03:00Z')
    monkeypatch.setattr(zotero_sqlite.sqlite_config, 'db_path', str(path))
    refresh = Mock()
    monkeypatch.setattr(zotero_sqlite, '_maybe_schedule_shadow_refresh', refresh)
    connection = await zotero_sqlite._get_connection()
    try:
        cursor = await connection.execute('SELECT libraryID FROM libraries')
        assert (await cursor.fetchone())[0] == 1
    finally:
        await connection.close()
    assert refresh.call_count == (0 if snapshot else 1)


def test_readonly_backup_captures_uncheckpointed_wal_without_changing_source(tmp_path):
    source=tmp_path/'live.sqlite';snapshot=tmp_path/'snapshot.sqlite'
    writer=sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute('CREATE TABLE papers(title TEXT)');writer.commit()
    writer.execute("INSERT INTO papers VALUES('A newly synced paper')");writer.commit()
    assert source.with_name(source.name+'-wal').stat().st_size>0
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as reader:
        with sqlite3.connect(snapshot) as destination:
            reader.backup(destination)
    with sqlite3.connect(snapshot) as db:
        assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert db.execute('SELECT title FROM papers').fetchone()[0]=='A newly synced paper'
    assert writer.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    writer.close()
