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
