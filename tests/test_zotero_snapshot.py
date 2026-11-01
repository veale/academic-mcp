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
