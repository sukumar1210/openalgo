"""
The strategy registry and config versions (PRD UAT-001 to UAT-003).

What matters here is immutability. A run's result is only meaningful next to
the exact strategy code and configuration that produced it, so the tests pin
the three ways that link could silently break: a changed strategy overwriting
its old version, a changed configuration overwriting its old version, and any
code path that edits a version row in place.
"""

import pytest

from research import strategy as rs

CONFIG = {
    "capital": 500000,
    "allocation": 1.0,
    "leverage": 1.0,
    "product": "MIS",
    "entry_time": "09:16",
    "exit_time": "15:10",
    "universe": ["RELIANCE", "INFY"],
}


class ArtifactStrategy:
    """A stand-in for a packaged strategy that carries a frozen model."""

    interval = "5m"
    artifact_hash = "a" * 64
    metadata = {"release": "test-1"}

    def decide(self, ctx):
        return None


class BrokenStrategy:
    def __init__(self):
        raise RuntimeError("artifact missing")


@pytest.fixture
def svc(research_db):
    from services import research_service

    return research_service


def _spec(key, attr):
    return rs.StrategySpec(key, "entry_point", f"{__name__}:{attr}", "test-dist", "1.0")


def test_builtin_reference_strategy_is_registered_once(svc, research_db):
    svc.sync_registry()
    svc.sync_registry()

    strategies = research_db.ResearchStrategy.query.all()
    assert [s.key for s in strategies] == ["first_bar_leader"]
    versions = research_db.ResearchStrategyVersion.query.all()
    assert len(versions) == 1
    assert versions[0].interval == "5m"
    assert len(versions[0].code_hash) == 64


def test_a_changed_artifact_is_a_new_version_and_the_old_one_is_kept(svc, research_db, monkeypatch):
    monkeypatch.setattr(rs, "discover", lambda: {"alpha": _spec("alpha", "ArtifactStrategy")})
    svc.sync_registry()
    first = research_db.ResearchStrategyVersion.query.one()
    first_hash = first.version_hash
    assert first.artifact_hash == "a" * 64
    assert first.meta == {"release": "test-1"}

    monkeypatch.setattr(ArtifactStrategy, "artifact_hash", "b" * 64)
    svc.sync_registry()

    versions = research_db.ResearchStrategyVersion.query.order_by(
        research_db.ResearchStrategyVersion.id
    ).all()
    assert [v.artifact_hash for v in versions] == ["a" * 64, "b" * 64]
    assert versions[0].version_hash == first_hash


def test_a_strategy_that_fails_to_load_is_reported_and_does_not_hide_others(svc, monkeypatch):
    specs = {
        "alpha": _spec("alpha", "ArtifactStrategy"),
        "broken": _spec("broken", "BrokenStrategy"),
    }
    monkeypatch.setattr(rs, "discover", lambda: specs)

    result = svc.list_strategies()

    assert [item["key"] for item in result["items"]] == ["alpha"]
    assert [p["key"] for p in result["problems"]] == ["broken"]
    assert "artifact missing" not in result["problems"][0]["message"]


def test_config_versions_grow_only_when_something_changed(svc, research_db):
    svc.sync_registry()
    strategy_id = research_db.ResearchStrategy.query.one().id

    v1, created = svc.save_config(strategy_id, CONFIG)
    assert created and v1["version_no"] == 1

    same, created = svc.save_config(strategy_id, dict(CONFIG, universe=["INFY", "reliance"]))
    assert not created and same["id"] == v1["id"]

    v2, created = svc.save_config(strategy_id, dict(CONFIG, leverage=2.0), parent_id=v1["id"])
    assert created and v2["version_no"] == 2 and v2["parent_id"] == v1["id"]
    assert v2["config_hash"] != v1["config_hash"]

    old = research_db.db_session.get(research_db.ResearchConfigVersion, v1["id"])
    assert old.config["leverage"] == 1.0
    assert old.config_hash == v1["config_hash"]


def test_version_rows_refuse_to_be_edited(svc, research_db):
    svc.sync_registry()
    strategy_id = research_db.ResearchStrategy.query.one().id
    v1, _ = svc.save_config(strategy_id, CONFIG)
    session = research_db.db_session

    config_row = session.get(research_db.ResearchConfigVersion, v1["id"])
    config_row.note = "edited"
    with pytest.raises(research_db.ImmutableRecordError):
        session.commit()
    session.rollback()

    version_row = research_db.ResearchStrategyVersion.query.one()
    version_row.code_hash = "0" * 64
    with pytest.raises(research_db.ImmutableRecordError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"capital": 0}, "Capital"),
        ({"allocation": 1.5}, "100%"),
        ({"product": "NRML"}, "MIS"),
        ({"exit_time": None}, "exit time"),
        ({"exit_time": "09:00"}, "after entry"),
        ({"universe": []}, "universe"),
        ({"stop_pct": 0}, "Stop"),
        ({"warp_speed": 9}, "Unknown"),
    ],
)
def test_invalid_configs_are_refused_with_a_readable_reason(svc, research_db, change, fragment):
    svc.sync_registry()
    strategy_id = research_db.ResearchStrategy.query.one().id

    with pytest.raises(svc.ResearchError, match=fragment):
        svc.save_config(strategy_id, dict(CONFIG, **change))
    assert research_db.ResearchConfigVersion.query.count() == 0


class Plain:
    interval = "5m"
    artifact_hash = "a1"

    def decide(self, ctx):
        return None


class Declared(Plain):
    model_hash = "m1"
    adapter_version = "0.3"

    def metadata(self):
        return {"release": "r1"}


def test_model_hash_and_adapter_version_join_identity_only_when_declared():
    def identity(target):
        return rs.version_identity(rs.StrategySpec("p", "entry_point", target, "pkg", "1"))

    plain = identity(f"{__name__}:Plain")
    base = {k: plain[k] for k in ("key", "source", "dist_name", "dist_version", "code_hash",
                                  "artifact_hash", "interval")}  # fmt: skip
    assert plain["version_hash"] == rs.sha256_hex(rs.canonical_json(base)), "old hashes hold"
    assert plain["model_hash"] is None and plain["adapter_version"] is None

    declared = identity(f"{__name__}:Declared")
    assert (declared["model_hash"], declared["adapter_version"]) == ("m1", "0.3")
    assert declared["version_hash"] != plain["version_hash"]
    assert declared["meta"] == {"release": "r1"}, "metadata may be a method"


def test_scenario_portfolio_limits_are_validated_and_kept(research_db):
    from services import research_service as svc

    svc.sync_registry()
    rdb = research_db
    strategy = rdb.ResearchStrategy.query.first()
    version = rdb.ResearchStrategyVersion.query.filter_by(strategy_id=strategy.id).first()
    config, _ = svc.save_config(strategy.id, {"capital": 100000, "universe": ["AAA"]})
    exp = svc.create_experiment("limits")["id"]

    def scenario(policy):
        return svc.create_scenario(
            exp,
            {"name": "s", "mode": "SHARED", "start": "2024-01-01", "end": "2024-01-02",
             "instances": [{"strategy_version_id": version.id, "config_version_id": config["id"]}],
             "risk_policy": policy},
        )  # fmt: skip

    kept = scenario({"daily_loss_limit": "5000", "lock_profit_at": 3000, "lock_trail_step": 500})
    assert kept["risk_policy"] == {
        "max_gross_leverage": 1.0, "daily_loss_limit": 5000.0,
        "lock_profit_at": 3000.0, "lock_trail_step": 500.0,
    }  # fmt: skip
    for bad in ({"daily_loss_limit": -1}, {"lock_trail_step": 100},
                {"lock_profit_at": 1000, "lock_profit_floor": 2000}):  # fmt: skip
        with pytest.raises(svc.ResearchError):
            scenario(bad)
