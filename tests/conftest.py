"""Pytest configuration and fixtures for academic-mcp tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock

# Import the modules we're testing
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def clear_field_ids_cache():
    """Clear the _field_ids cache between tests to ensure isolation."""
    from academic_mcp import zotero_sqlite
    zotero_sqlite._field_ids.clear()
    yield
    zotero_sqlite._field_ids.clear()


@pytest.fixture
def mock_aiosqlite_connection():
    """Create a mock aiosqlite connection with proper row factory."""
    conn = AsyncMock()
    conn.row_factory = MagicMock()
    
    # Create a mock cursor
    cursor = AsyncMock()
    conn.execute = AsyncMock(return_value=cursor)
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.fetchone = AsyncMock(return_value=None)
    
    return conn


@pytest.fixture
def sample_zotero_item():
    """Sample ZoteroItem for testing."""
    from academic_mcp.models import ZoteroItem, Creator
    
    return ZoteroItem(
        itemID=12345,
        key="ABCD1234",
        libraryID=1,
        libraryName="My Library",
        libraryType="user",
        itemType="journalArticle",
        title="Test Paper Title",
        DOI="10.1234/test.doi",
        url="https://example.com/paper",
        date="2024-01-01",
        abstractNote="This is a test abstract.",
        publicationTitle="Test Journal",
        creators=[
            Creator(firstName="John", lastName="Doe", creatorType="author")
        ],
        tags=["test", "sample"],
        extra="",
        dateAdded="2024-01-01T00:00:00+00:00",
        dateModified="2024-01-01T00:00:00+00:00",
    )


@pytest.fixture
def sample_pdf_bytes():
    """Sample PDF bytes for testing."""
    # Minimal valid PDF header + small content
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"


@pytest.fixture
def sample_pdf_base64():
    """Sample base64-encoded PDF for testing."""
    import base64
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
    return base64.b64encode(pdf_bytes).decode("ascii")
