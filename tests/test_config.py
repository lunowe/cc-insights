import pathlib

from cc_insights import config as config_mod
from cc_insights import paths


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
    assert set(cfg.source_globs) == {"claude_code", "codex"}
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


# ------------------------------------------------------------------ windows --


def test_a_windows_db_path_round_trips_through_toml(tmp_path):
    """`\\U` and `\\A` are TOML escape sequences, and a Windows path is full of them.

    Unescaped, `C:\\Users\\you\\AppData\\...` makes the config file this code
    just wrote unparseable on the very next run -- the host_id becomes
    unreadable, and a regenerated host_id forks the entire history.
    """
    import tomllib

    cfg = config_mod.load(tmp_path)
    cfg.db_path = pathlib.PureWindowsPath(r"C:\Users\you\AppData\Roaming\cc-insights\cci.db")
    written = cfg.to_toml()

    assert tomllib.loads(written)["db_path"] == str(cfg.db_path)


def test_a_glob_with_an_undefined_variable_survives_verbatim():
    """A Windows-only pattern read on a Mac must match nothing, not explode."""
    assert config_mod.expand_glob("%NOT_A_REAL_VAR%/x/*.jsonl") == "%NOT_A_REAL_VAR%/x/*.jsonl"


def test_expand_glob_resolves_home_and_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CCI_TEST_ROOT", str(tmp_path))
    assert config_mod.expand_glob("$CCI_TEST_ROOT/x") == f"{tmp_path}/x"
    assert not config_mod.expand_glob("~/x").startswith("~")


def test_windows_gets_extra_source_locations(monkeypatch):
    """The shipped globs grow on Windows and are untouched everywhere else."""
    monkeypatch.setattr(paths, "LOCAL", paths.POSIX)
    assert config_mod.default_source_globs() == config_mod.DEFAULT_SOURCE_GLOBS

    monkeypatch.setattr(paths, "LOCAL", paths.WINDOWS)
    on_windows = config_mod.default_source_globs()
    for source, patterns in config_mod.DEFAULT_SOURCE_GLOBS.items():
        assert set(patterns) <= set(on_windows[source]), "the defaults must never be lost"
    assert any("%APPDATA%" in p for p in on_windows["claude_code"])


def test_the_config_dir_follows_the_platform(monkeypatch):
    monkeypatch.delenv("CC_INSIGHTS_HOME", raising=False)
    monkeypatch.setattr(paths, "LOCAL", paths.WINDOWS)
    monkeypatch.setenv("APPDATA", r"C:\Users\you\AppData\Roaming")
    assert config_mod.default_config_dir().name == "cc-insights"
    assert "AppData" in str(config_mod.default_config_dir())

    monkeypatch.setattr(paths, "LOCAL", paths.POSIX)
    assert config_mod.default_config_dir() == pathlib.Path("~/.config/cc-insights").expanduser()


def test_cc_insights_home_still_wins_on_every_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_INSIGHTS_HOME", str(tmp_path))
    for flavor in (paths.WINDOWS, paths.POSIX):
        monkeypatch.setattr(paths, "LOCAL", flavor)
        assert config_mod.default_config_dir() == tmp_path
