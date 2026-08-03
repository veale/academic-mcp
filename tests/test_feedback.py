import pytest

from academic_mcp import feedback


@pytest.fixture()
def feedback_db(tmp_path, monkeypatch):
    path = tmp_path / "feedback.sqlite3"
    monkeypatch.setenv("FEEDBACK_DB_PATH", str(path))
    return path


def test_feedback_lifecycle(feedback_db):
    created = feedback.submit({
        "summary": "Search timed out",
        "details": "search_papers stopped after the client timeout",
        "category": "reliability",
        "severity": "high",
        "tool_name": "search_papers",
        "reproduction_steps": ["Call search_papers", "Wait 60 seconds"],
        "client": "test-client/1",
    })

    assert created["id"].startswith("fb_")
    assert feedback_db.exists()
    assert feedback.list_items() == [{
        "id": created["id"],
        "created_at": created["created_at"],
        "updated_at": created["created_at"],
        "status": "open",
        "category": "reliability",
        "severity": "high",
        "summary": "Search timed out",
        "tool_name": "search_papers",
    }]

    item = feedback.get(created["id"])
    assert item["reproduction_steps"] == ["Call search_papers", "Wait 60 seconds"]

    feedback.update(created["id"], "resolved", "Fixed timeout handling")
    assert feedback.list_items() == []
    assert feedback.get(created["id"])["resolution"] == "Fixed timeout handling"


def test_feedback_validates_required_fields(feedback_db):
    with pytest.raises(ValueError, match="summary and details"):
        feedback.submit({"summary": "missing details"})


def test_feedback_rejects_unknown_status(feedback_db):
    with pytest.raises(ValueError, match="status must be one of"):
        feedback.list_items("mystery")
