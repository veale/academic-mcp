"""Zotero personal-library WebDAV attachment publishing.

Publish K.zip before K.prop, then update the Web API attachment metadata.
The directory URL has the same meaning as in the existing WebDAV reader:
it already points to the Zotero storage directory, including /zotero/.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from .zotero import zot_config


def enabled() -> bool:
    # Zotero group libraries always use Zotero File Storage.
    return zot_config.library_type != 'group' and bool(zot_config.webdav_url)


def _url(key: str, suffix: str) -> str:
    return urljoin(zot_config.webdav_url.rstrip('/') + '/', f'{key}.{suffix}')


def _auth():
    return (zot_config.webdav_user, zot_config.webdav_pass) if zot_config.webdav_user else None


async def has_file(key: str, data: dict) -> bool:
    """Check the actual desktop sync backend, not just Zotero item metadata."""
    if not data.get('md5'):
        return False
    async with httpx.AsyncClient(timeout=30, auth=_auth()) as client:
        prop = await client.get(_url(key, 'prop'))
        if prop.status_code == 404:
            return False
        prop.raise_for_status()
        try:
            root = ElementTree.fromstring(prop.content)
            if root.findtext('hash') != data['md5']:
                return False
            if int(root.findtext('mtime') or 0) != int(data.get('mtime') or 0):
                return False
        except (ElementTree.ParseError, ValueError):
            return False
        archive = await client.head(_url(key, 'zip'))
        if archive.status_code == 404:
            return False
        archive.raise_for_status()
        return True


async def _stream_file(path: Path):
    with path.open('rb') as stream:
        while chunk := stream.read(64 * 1024):
            yield chunk


async def upload(key: str, pdf_path: Path, data: dict) -> tuple[bool, str]:
    """Publish/recover an existing attachment key; never replace a remote file.

    On an already published file the metadata hash must match the local PDF.
    Conditional PUTs protect against a concurrent desktop upload. Metadata is
    only published after both WebDAV objects have reached the server.
    """
    if not enabled():
        return False, 'WebDAV is not configured for this personal library'
    filename = data.get('filename') or pdf_path.name
    if Path(filename).name != filename or '/' in filename or '\\' in filename:
        return False, 'unsafe attachment filename'
    def digest_file():
        digest = hashlib.md5(usedforsecurity=False)
        with pdf_path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    digest = await asyncio.to_thread(digest_file)
    # For repairs retain the original metadata time so desktop and .prop agree.
    mtime = int(data.get('mtime') or int(pdf_path.stat().st_mtime * 1000))
    if data.get('md5') and data['md5'] != digest:
        return False, 'local PDF differs from attachment metadata; refusing overwrite'
    prop_xml = f'<properties version="1"><mtime>{mtime}</mtime><hash>{digest}</hash></properties>'
    try:
        with tempfile.TemporaryDirectory(prefix='academic-webdav-') as temporary:
            archive = Path(temporary) / f'{key}.zip'
            def create_zip():
                with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(pdf_path, arcname=filename)
            await asyncio.to_thread(create_zip)
            async with httpx.AsyncClient(timeout=120, auth=_auth()) as client:
                # Never overwrite an existing archive, including one a desktop
                # has just uploaded. Read and validate it before publishing .prop.
                head = await client.head(_url(key, 'zip'))
                if head.status_code == 404:
                    response = await client.put(
                        _url(key, 'zip'), content=_stream_file(archive),
                        headers={'Content-Type': 'application/zip', 'Content-Length': str(archive.stat().st_size), 'If-None-Match': '*'},
                    )
                    if response.status_code not in (200, 201, 204):
                        return False, f'WebDAV ZIP upload failed ({response.status_code})'
                elif head.status_code == 200:
                    # Existing files can be original desktop uploads, encoded
                    # ZIPs, or a newer revision. Matching .prop is authoritative;
                    # otherwise leave them untouched for manual conflict handling.
                    current = await client.get(_url(key, 'prop'))
                    if current.status_code == 200:
                        root = ElementTree.fromstring(current.content)
                        if root.findtext('hash') != digest or int(root.findtext('mtime') or 0) != mtime:
                            return False, 'WebDAV has different file metadata; refusing overwrite'
                    elif current.status_code != 404:
                        return False, f'WebDAV properties read failed ({current.status_code})'
                    else:
                        # A previous attempt may have stopped after publishing
                        # the ZIP. Validate the actual payload before completing it.
                        downloaded = Path(temporary) / 'existing.zip'
                        async with client.stream('GET', _url(key, 'zip')) as response:
                            response.raise_for_status()
                            with downloaded.open('wb') as out:
                                async for chunk in response.aiter_bytes():
                                    out.write(chunk)
                        with zipfile.ZipFile(downloaded) as zf:
                            if filename not in zf.namelist():
                                return False, 'existing WebDAV ZIP has a different filename'
                            check = hashlib.md5(usedforsecurity=False)
                            with zf.open(filename) as stream:
                                while chunk := stream.read(1024 * 1024):
                                    check.update(chunk)
                            if check.hexdigest() != digest:
                                return False, 'existing WebDAV ZIP differs from local PDF'
                else:
                    return False, f'WebDAV ZIP check failed ({head.status_code})'
                response = await client.put(
                    _url(key, 'prop'), content=prop_xml.encode(), headers={'Content-Type': 'text/xml'},
                )
                if response.status_code not in (200, 201, 204):
                    return False, f'WebDAV properties upload failed ({response.status_code})'
            # Keep Zotero credentials off WebDAV requests, and vice versa.
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.patch(
                    f'https://api.zotero.org{zot_config.library_prefix}/items/{key}',
                    json={'md5': digest, 'mtime': mtime, 'filename': filename},
                    headers={'Zotero-API-Key': zot_config.api_key, 'If-Unmodified-Since-Version': str(data['version'])},
                )
                if response.status_code != 204:
                    return False, f'Zotero WebDAV metadata update failed ({response.status_code})'
        return True, ''
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, httpx.RequestError, httpx.HTTPStatusError) as exc:
        # Exceptions can contain credential-bearing WebDAV URLs. Keep errors
        # actionable without returning URLs, bodies or credentials to clients.
        return False, f'WebDAV upload failed: {type(exc).__name__}'
