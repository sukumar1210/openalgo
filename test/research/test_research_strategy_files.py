"""
Uploaded strategy files: a strategy as one Python file, added from /research.

The file is a trader's code, so what matters is where it runs and what a bad
one leaves behind. It is read in a process of its own, never in the app; one
that does not build a strategy is refused with what to fix and leaves nothing
on disk; and replacing it is a new version, never an edit of the old one.
"""

import pytest

from research import strategy as rs

GOOD = b"""
class Strategy:
    interval = "5m"

    def decide(self, ctx):
        return None
"""


@pytest.fixture
def svc(research_db, tmp_path, monkeypatch):
    from services import research_service

    monkeypatch.setattr(rs, "STRATEGY_FILES", tmp_path / "strategies" / "research")
    return research_service


def test_an_uploaded_file_becomes_a_strategy_the_workers_can_build(svc, research_db):
    item = svc.upload_strategy_file("opening_range.py", GOOD)

    spec = rs.discover()["opening_range"]
    assert spec.source == "file"
    # The worker's check: what it builds is exactly the version recorded.
    assert rs.version_identity(spec)["version_hash"] == item["version_hash"]
    assert rs.build(spec).interval == "5m"
    row = research_db.db_session.get(research_db.ResearchStrategy, item["id"])
    assert (row.key, row.source) == ("opening_range", "file")


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (
            b"class Strategy:\n    interval = '5m'\n    def decide(self, ctx)\n",
            "syntax error on line 3",
        ),
        (b"import not_a_package_here\n", "imports not_a_package_here"),
        (b"class Other:\n    pass\n", "no class named Strategy"),
        (
            b"class Strategy:\n    interval = '2m'\n    def decide(self, ctx):\n        return None\n",
            "unsupported bar interval",
        ),
        (
            b"import socket\nsocket.create_connection(('127.0.0.1', 9))\n",
            "open a network connection",
        ),
    ],
)
def test_a_file_that_does_not_build_a_strategy_is_refused_and_leaves_nothing(
    svc, content, fragment
):
    with pytest.raises(svc.ResearchError, match=fragment):
        svc.upload_strategy_file("broken.py", content)
    assert list(rs.STRATEGY_FILES.iterdir()) == []
    assert "broken" not in rs.discover()


def test_replacing_a_file_is_a_new_version_and_the_old_one_is_kept(svc, research_db):
    first = svc.upload_strategy_file("opening_range.py", GOOD)
    with pytest.raises(svc.ResearchError, match="already here"):
        svc.upload_strategy_file("opening_range.py", GOOD + b"\n# v2\n")

    second = svc.upload_strategy_file("opening_range.py", GOOD + b"\n# v2\n", replace=True)

    assert second["id"] == first["id"]
    assert second["version_hash"] != first["version_hash"]
    versions = research_db.db_session.query(research_db.ResearchStrategyVersion).filter_by(
        strategy_id=first["id"]
    )
    assert {v.version_hash for v in versions} == {first["version_hash"], second["version_hash"]}


@pytest.mark.parametrize(
    ("filename", "fragment"),
    [
        ("strategy.txt", "ending in .py"),
        ("Opening Range.py", "lowercase letters"),
        ("first_bar_leader.py", "already the name of an installed strategy"),
    ],
)
def test_a_file_name_must_be_a_new_key(svc, filename, fragment):
    with pytest.raises(svc.ResearchError, match=fragment):
        svc.upload_strategy_file(filename, GOOD)


def test_the_registry_reads_a_known_file_without_starting_a_process(svc, monkeypatch):
    svc.upload_strategy_file("opening_range.py", GOOD)

    def refuse(*_args):
        raise AssertionError("a known file was checked again")

    monkeypatch.setattr(svc, "identify_file", refuse)
    assert svc.sync_registry() == []


def test_a_file_changed_on_disk_is_checked_and_a_broken_one_is_reported(svc):
    svc.upload_strategy_file("opening_range.py", GOOD)
    (rs.STRATEGY_FILES / "opening_range.py").write_bytes(b"class Strategy:\n    pass\n")

    problems = svc.sync_registry()

    assert [p["key"] for p in problems] == ["opening_range"]
    assert "interval" in problems[0]["message"]
