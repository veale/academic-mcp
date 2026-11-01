"""Verify Zotero's actual desktop file-sync objects, ordering and conflicts."""
import hashlib
import io
import zipfile

import httpx
import pytest

from academic_mcp import zotero_webdav as dav


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.setattr(dav.zot_config, 'library_type', 'user')
    monkeypatch.setattr(dav.zot_config, 'webdav_url', 'https://dav.example/zotero/')
    monkeypatch.setattr(dav.zot_config, 'webdav_user', 'dav-user')
    monkeypatch.setattr(dav.zot_config, 'webdav_pass', 'dav-secret')
    monkeypatch.setattr(dav.zot_config, 'api_key', 'api-secret')
    monkeypatch.setattr(dav.zot_config, 'user_id', '123')
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'%PDF-1.4\nPDF payload')
    data = {'filename': 'Horváth - 2025 - Paper.pdf', 'version': 12, 'mtime': 1700000000000,
            'md5': hashlib.md5(path.read_bytes()).hexdigest()}
    return path, data


def mock_client(monkeypatch, handler):
    client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(dav.httpx, 'AsyncClient', lambda **kwargs: client(transport=transport, **kwargs))


async def test_publishes_zip_then_prop_then_versioned_metadata(backend, monkeypatch):
    path, data = backend
    calls = []
    async def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.host == 'dav.example':
            assert request.headers['Authorization'].startswith('Basic ')
            assert 'Zotero-API-Key' not in request.headers
        else:
            assert request.headers['Zotero-API-Key'] == 'api-secret'
            assert 'Authorization' not in request.headers
            assert request.headers['If-Unmodified-Since-Version'] == '12'
        if request.method == 'HEAD':
            return httpx.Response(404)
        body = await request.aread()
        if request.url.path.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                assert zf.namelist() == [data['filename']]
                assert zf.read(data['filename']) == path.read_bytes()
            assert request.headers['If-None-Match'] == '*'
            return httpx.Response(201)
        if request.url.path.endswith('.prop'):
            root = dav.ElementTree.fromstring(body)
            assert root.findtext('hash') == data['md5']
            assert root.findtext('mtime') == str(data['mtime'])
            return httpx.Response(201)
        return httpx.Response(204)
    mock_client(monkeypatch, handler)
    assert await dav.upload('ATTACH12', path, data) == (True, '')
    assert [method for method, _ in calls] == ['HEAD', 'PUT', 'PUT', 'PATCH']


async def test_failed_zip_does_not_publish_prop_or_metadata(backend, monkeypatch):
    path, data = backend
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404 if request.method == 'HEAD' else 507)
    mock_client(monkeypatch, handler)
    ok, error = await dav.upload('ATTACH12', path, data)
    assert not ok and '507' in error
    assert all(p.endswith('.zip') for p in calls)


async def test_upload_refuses_different_remote_revision(backend, monkeypatch):
    path, data = backend
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, content=b'<properties><hash>different</hash><mtime>1700000000000</mtime></properties>')
    mock_client(monkeypatch, handler)
    ok, error = await dav.upload('ATTACH12', path, data)
    assert not ok and 'refusing overwrite' in error
    assert calls == ['HEAD', 'GET']


async def test_metadata_is_not_proof_of_webdav_file(backend, monkeypatch):
    _, data = backend
    def handler(request):
        if request.method == 'HEAD':
            return httpx.Response(404)
        return httpx.Response(200, content=f'<properties><hash>{data["md5"]}</hash><mtime>{data["mtime"]}</mtime></properties>'.encode())
    mock_client(monkeypatch, handler)
    assert not await dav.has_file('ATTACH12', data)


def test_groups_do_not_use_personal_webdav(backend, monkeypatch):
    monkeypatch.setattr(dav.zot_config, 'library_type', 'group')
    assert not dav.enabled()
