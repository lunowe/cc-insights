"""Tests for filling a column a later migration added.

The reason this exists rather than "just rebuild the database": agent log
directories are pruned on a rolling basis, so a rebuild drops every session
whose log has aged out. A database is allowed to hold history its source no
longer does, which is the entire point of the project.
"""

import json
from pathlib import Path

import pytest

from cc_insights import backfill, db, ids, ingest
from cc_insights.config import Config

CLAUDE = "claude_code"


def make_config(tmp_path: Path, **globs) -> Config:
    return Config(host_id="h", hostname="H", db_path=tmp_path / "test.db",
                  source_globs={k: [str(p) for p in v] for k, v in globs.items()},
                  config_dir=tmp_path)


def line(uuid: str, ts: str, *, cw: int, cw1h: int | None) -> str:
    usage = {"input_tokens": 5, "output_tokens": 5, "cache_creation_input_tokens": cw}
    if cw1h is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cw - cw1h,
            "ephemeral_1h_input_tokens": cw1h,
        }
    return json.dumps({
        "isSidechain": False, "cwd": "/code/demo", "sessionId": "s1",
        "version": "2.0.0", "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5", "content": [],
                    "usage": usage},
        "uuid": uuid, "timestamp": ts,
    }) + "\n"


@pytest.fixture
def corpus(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s1.jsonl").write_text(
        line("u1", "2026-05-10T09:00:00.000Z", cw=1000, cw1h=400)
        + line("u2", "2026-05-10T09:01:00.000Z", cw=500, cw1h=0))
    cfg = make_config(tmp_path, **{CLAUDE: [logs / "*.jsonl"]})
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    yield conn, cfg, logs
    conn.close()


def blank_the_column(conn):
    """The state every database built before migration 005 is in."""
    conn.execute("UPDATE event SET cache_write_1h_tokens = NULL")


def values(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT native_event_id, cache_write_1h_tokens FROM event ORDER BY 1")}


def test_it_fills_a_column_the_logs_still_know(corpus):
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    blank_the_column(conn)
    assert set(values(conn).values()) == {None}

    stats = backfill.backfill(conn, cfg)
    assert stats.filled == 2
    assert values(conn) == {"u1": 400, "u2": 0}


def test_zero_is_filled_as_zero_not_left_unknown(corpus):
    """`0` means the request wrote five-minute entries only, which is a fact.
    Leaving it NULL would report it as an assumption forever."""
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    blank_the_column(conn)
    backfill.backfill(conn, cfg)
    assert values(conn)["u2"] == 0


def test_a_second_run_fills_nothing(corpus):
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    blank_the_column(conn)
    backfill.backfill(conn, cfg)
    again = backfill.backfill(conn, cfg)
    assert again.filled == 0 and again.already_set == 2


def test_it_never_inserts_a_row(corpus):
    """An event only in the logs is `cci ingest`'s job, not this one. Filling
    it here would write a row with no session and no thread."""
    conn, cfg, logs = corpus
    ingest.ingest(conn, cfg)
    before = conn.execute("SELECT count(*) FROM event").fetchone()[0]
    with (logs / "s1.jsonl").open("a") as fh:
        fh.write(line("u3", "2026-05-10T09:02:00.000Z", cw=900, cw1h=900))

    stats = backfill.backfill(conn, cfg)
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == before
    assert stats.not_in_database == 1


def test_it_touches_no_other_column(corpus):
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    snapshot = [tuple(r) for r in conn.execute(
        "SELECT id, ts, model, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens FROM event ORDER BY id")]
    blank_the_column(conn)
    backfill.backfill(conn, cfg)
    after = [tuple(r) for r in conn.execute(
        "SELECT id, ts, model, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens FROM event ORDER BY id")]
    assert after == snapshot


def test_a_log_that_never_reported_the_split_leaves_it_unknown(tmp_path: Path):
    """What has aged out, and every Codex event, stays NULL -- which `cci
    cost` reports as an assumption rather than absorbing."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s1.jsonl").write_text(line("u1", "2026-05-10T09:00:00.000Z", cw=1000, cw1h=None))
    cfg = make_config(tmp_path, **{CLAUDE: [logs / "*.jsonl"]})
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        ingest.ingest(conn, cfg)
        stats = backfill.backfill(conn, cfg)
        assert stats.filled == 0
        assert values(conn) == {"u1": None}
    finally:
        conn.close()


def test_coverage_is_measured_in_tokens_not_rows(corpus):
    """One busy event can carry more cache writes than a thousand quiet ones,
    and it is the tokens that carry the money."""
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    blank_the_column(conn)
    assert backfill.coverage(conn, "cache_write_1h_tokens") == (0, 1500)
    backfill.backfill(conn, cfg)
    assert backfill.coverage(conn, "cache_write_1h_tokens") == (1500, 1500)


def test_ids_are_recomputed_not_looked_up(corpus):
    """The property that makes this safe: a parsed event knows which row it
    is, because every id is a hash of its natural key."""
    conn, cfg, _ = corpus
    ingest.ingest(conn, cfg)
    session = ids.session_id(cfg.host_id, CLAUDE, "s1")
    assert conn.execute("SELECT count(*) FROM event WHERE id = ?",
                        (ids.event_id(session, "u1"),)).fetchone()[0] == 1


def test_every_job_explains_itself():
    """A backfill rewrites stored data. Each one says why, in the table."""
    assert backfill.JOBS
    for job in backfill.JOBS:
        assert job.column and job.attribute and len(job.why) > 20
