from cc_insights import config as config_mod


def test_first_run_generates_and_persists_host_id(tmp_path):
    cfg = config_mod.load(tmp_path)
    assert cfg.path.exists()
    assert config_mod.load(tmp_path).host_id == cfg.host_id, "host_id must be stable"


def test_config_round_trips(tmp_path):
    cfg = config_mod.load(tmp_path)
    cfg.idle_threshold_s = 120
    cfg.save()
    again = config_mod.load(tmp_path)
    assert again.idle_threshold_s == 120
    assert again.db_path == cfg.db_path
    assert again.source_globs == cfg.source_globs


def test_defaults(tmp_path):
    cfg = config_mod.load(tmp_path)
    assert cfg.idle_threshold_s == 300
    assert set(cfg.source_globs) == {"claude_code", "codex", "opencode"}
    assert cfg.globs_for("codex") and all(not str(p).startswith("~") for p in cfg.globs_for("codex"))


def test_a_copied_config_dir_points_at_its_own_database(tmp_path):
    """Copying a config dir to experiment on must not write to the original.

    With an absolute db_path in the TOML the copy silently keeps pointing at
    the source database, so `--config-dir <copy>` mutates the real one. That
    happened and corrupted two rows of a committed fixture.
    """
    import shutil

    from cc_insights import config as config_mod

    src = tmp_path / "src"
    original = config_mod.load(src)
    original.db_path.write_bytes(b"")  # the DB itself need not be valid here

    copy = tmp_path / "copy"
    shutil.copytree(src, copy)

    reloaded = config_mod.load(copy, create=False)
    assert reloaded.db_path.parent == copy, (
        f"copy resolves to {reloaded.db_path}, which is not inside {copy}"
    )
    assert reloaded.db_path != original.db_path


def test_a_database_deliberately_outside_the_config_dir_stays_absolute(tmp_path):
    from cc_insights import config as config_mod

    cfg = config_mod.load(tmp_path / "cfg")
    elsewhere = tmp_path / "elsewhere" / "custom.db"
    cfg.db_path = elsewhere
    cfg.save()
    assert f'db_path = "{elsewhere}"' in cfg.path.read_text()
    assert config_mod.load(tmp_path / "cfg", create=False).db_path == elsewhere


def test_a_config_written_before_a_source_existed_still_finds_it(tmp_path):
    """Adding an adapter must not require editing every config on disk.

    Without the merge the new source discovers nothing and says nothing about
    it, which is the worst failure mode available.
    """
    (tmp_path / "config.toml").write_text(
        'host_id = "h"\nhostname = "H"\ndb_path = "x.db"\n\n'
        '[source_globs]\nclaude_code = ["~/custom/*.jsonl"]\n'
    )
    cfg = config_mod.load(tmp_path)
    assert cfg.source_globs["claude_code"] == ["~/custom/*.jsonl"]  # the file wins
    assert cfg.source_globs["opencode"] == config_mod.DEFAULT_SOURCE_GLOBS["opencode"]
    assert set(cfg.source_globs) == set(config_mod.DEFAULT_SOURCE_GLOBS)
