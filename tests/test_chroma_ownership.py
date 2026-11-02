import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from academic_mcp import semantic_index


def test_one_embedded_client_owns_path_across_processes(tmp_path, monkeypatch):
    client = SimpleNamespace(get_or_create_collection=Mock(return_value="collection"))
    factory = Mock(return_value=client)
    monkeypatch.setitem(
        sys.modules, "chromadb", SimpleNamespace(PersistentClient=factory)
    )
    first = semantic_index.SemanticIndex()
    first.cache_dir = tmp_path
    second = semantic_index.SemanticIndex()
    second.cache_dir = tmp_path
    try:
        assert first._get_chroma_collection() == "collection"
        assert second._get_chroma_collection() == "collection"
        factory.assert_called_once()
        script = """import fcntl,sys
f=open(sys.argv[1],'a')
try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: sys.exit(0)
sys.exit(1)
"""
        assert (
            subprocess.run(
                [sys.executable, "-c", script, str(tmp_path / "client.lock")]
            ).returncode
            == 0
        )
    finally:
        _, owner, _ = semantic_index._chroma_clients.pop(str(tmp_path.resolve()))
        owner.close()
