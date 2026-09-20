from cc_insights import cli


def test_init_creates_config_and_db(tmp_path, capsys):
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    out = capsys.readouterr().out
    assert "host_id" in out
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "cc-insights.db").exists()


def test_init_is_idempotent(tmp_path, capsys):
    cli.main(["--config-dir", str(tmp_path), "init"])
    capsys.readouterr()
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_status_reports_empty_tables(tmp_path, capsys):
    cli.main(["--config-dir", str(tmp_path), "init"])
    capsys.readouterr()
    assert cli.main(["--config-dir", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "session" in out
    assert "schema versions" in out


def test_read_commands_refuse_an_out_of_date_database(tmp_path, capsys, monkeypatch):
    """A database written by an older cci must fail with the fix, not with
    `no such table` from deep inside a query.

    The old database is simulated by hiding the newest migration while `init`
    runs -- which is what an older release actually did -- rather than by
    deleting a schema_migrations row and leaving its tables behind, a state
    that cannot occur because each migration commits atomically.
    """
    import pytest

    from cc_insights import cli as cli_mod, db

    original = db.discover_migrations
    newest = max(v for v, _ in original())
    monkeypatch.setattr(
        db, "discover_migrations",
        lambda directory=None: [m for m in original(directory) if m[0] < newest],
    )
    assert cli_mod.main(["--config-dir", str(tmp_path), "init"]) == 0
    monkeypatch.setattr(db, "discover_migrations", original)
    capsys.readouterr()

    # `stats` reads tables the pending migration creates, so it refuses.
    with pytest.raises(SystemExit):
        cli_mod.main(["--config-dir", str(tmp_path), "stats"])
    assert "run `cci init` to migrate" in capsys.readouterr().err

    # `status` is the diagnostic command; it still runs and says so.
    assert cli_mod.main(["--config-dir", str(tmp_path), "status"]) == 0
    assert "schema is out of date" in capsys.readouterr().out

    # `cci init` is the fix the message names, so it must actually work.
    assert cli_mod.main(["--config-dir", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    assert cli_mod.main(["--config-dir", str(tmp_path), "stats"]) == 0
    assert "schema is out of date" not in capsys.readouterr().out
