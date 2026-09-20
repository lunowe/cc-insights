import pathlib

import pytest

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


# ------------------------------------------------- repairing an old config --
#
# `_db_path_for_toml` has written a relative db_path since the fix that
# introduced it, but that fix never rewrote the configs already on disk. On one
# of those, copying the config directory still silently writes to the original
# database. That has caused damage twice -- two corrupted rows in a committed
# fixture, and a migration applied to a live database during v2 -- so `cci
# init` now repairs it in place.


def write_config(config_dir, db_path, *, extra=""):
    """A config as an older release left it: db_path absolute."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        "# hand-written comment someone cares about\n"
        'host_id = "stable-host-id"\n'
        'hostname = "box"\n'
        f'db_path = "{db_path}"\n'
        "idle_threshold_s = 300\n"
        f"{extra}"
        "\n[source_globs]\n"
        'claude_code = ["~/custom/*.jsonl"]\n'
    )
    return config_dir / "config.toml"


def test_an_absolute_db_path_inside_its_own_config_dir_is_made_relative(tmp_path):
    write_config(tmp_path, tmp_path / "cc-insights.db")

    assert config_mod.make_db_path_portable(tmp_path) == tmp_path / "config.toml"

    assert 'db_path = "cc-insights.db"' in (tmp_path / "config.toml").read_text()
    assert config_mod.load(tmp_path, create=False).db_path == tmp_path / "cc-insights.db"


def test_the_repair_is_what_makes_a_copied_config_dir_safe(tmp_path):
    """The actual bug, end to end: a copy must write to the copy.

    Without the repair the copy's config still names the original database, so
    `--config-dir <copy>` mutates the thing you copied it to protect.
    """
    import shutil

    original = tmp_path / "original"
    write_config(original, original / "cc-insights.db")
    (original / "cc-insights.db").write_bytes(b"")

    config_mod.make_db_path_portable(original)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy)

    assert config_mod.load(copy, create=False).db_path == copy / "cc-insights.db"


def test_a_database_deliberately_elsewhere_is_left_absolute(tmp_path):
    """Only a path inside the config directory is a portability problem."""
    elsewhere = tmp_path / "elsewhere" / "custom.db"
    cfg_dir = tmp_path / "cfg"
    write_config(cfg_dir, elsewhere)
    before = (cfg_dir / "config.toml").read_text()

    assert config_mod.make_db_path_portable(cfg_dir) is None
    assert (cfg_dir / "config.toml").read_text() == before


def test_an_already_portable_config_is_not_rewritten(tmp_path):
    cfg = config_mod.load(tmp_path)
    before = cfg.path.read_text()

    assert config_mod.make_db_path_portable(tmp_path) is None
    assert cfg.path.read_text() == before, "no-op must not even reformat"


def test_the_repair_preserves_everything_it_did_not_come_for(tmp_path):
    """One line changes. Comments and keys this version does not model survive.

    Regenerating the file from the dataclass would be simpler and would drop
    both -- including any key a newer release added and this one has never
    heard of.
    """
    write_config(tmp_path, tmp_path / "cc-insights.db",
                 extra='some_future_key = "do not lose me"\n')

    config_mod.make_db_path_portable(tmp_path)

    text = (tmp_path / "config.toml").read_text()
    assert "# hand-written comment someone cares about" in text
    assert 'some_future_key = "do not lose me"' in text
    assert 'claude_code = ["~/custom/*.jsonl"]' in text
    assert config_mod.load(tmp_path, create=False).host_id == "stable-host-id"


def test_a_windows_db_path_survives_the_repair(tmp_path, monkeypatch):
    """The rewritten line goes through the same TOML escaping as everything else."""
    import tomllib

    monkeypatch.setattr(paths, "LOCAL", paths.WINDOWS)
    win_dir = tmp_path / "cfg"
    win_dir.mkdir()
    (win_dir / "config.toml").write_text(
        'host_id = "h"\nhostname = "box"\n'
        + 'db_path = '
        + config_mod._toml_str(str(win_dir / "cc-insights.db"))
        + "\n"
    )
    config_mod.make_db_path_portable(win_dir)
    assert tomllib.loads((win_dir / "config.toml").read_text())["db_path"] == "cc-insights.db"


def test_an_unparseable_config_is_left_for_a_human(tmp_path):
    """Not ours to repair, and guessing at it could lose the host_id."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    broken = tmp_path / "config.toml"
    broken.write_text("this is not = = toml\n")

    assert config_mod.make_db_path_portable(tmp_path) is None
    assert broken.read_text() == "this is not = = toml\n"


def test_init_repairs_an_old_config_and_says_so(tmp_path, capsys):
    from cc_insights import cli

    write_config(tmp_path, tmp_path / "cc-insights.db")

    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0

    out = capsys.readouterr().out
    assert "db_path made relative" in out
    assert 'db_path = "cc-insights.db"' in (tmp_path / "config.toml").read_text()
    # The host_id is the thing that must never change.
    assert config_mod.load(tmp_path, create=False).host_id == "stable-host-id"


def test_init_says_created_only_on_a_first_run(tmp_path, capsys):
    """`created_config` was computed after load(), which creates the file, so
    it was always False and the marker never appeared."""
    from cc_insights import cli

    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    assert "(created)" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    assert "(created)" not in capsys.readouterr().out


# ------------------------------------------ durability, and honest failure --


def test_the_config_is_flushed_to_disk_before_the_rename_and_after_it(tmp_path, monkeypatch):
    """`rename(2)` is atomic against other processes, not against power loss.

    Without an fsync on the file, the rename can reach the disk while the
    bytes it points at are still in the page cache, and the machine comes
    back up with a zero-length config.toml -- the rename durable, the data
    not. Without an fsync on the *directory*, the rename itself is what can
    be lost. Both are needed and neither is implied by the other.

    Checked by watching the syscalls, because the honest version of this test
    needs somebody to pull a power cord. `st_mode` is what makes it more than
    a call count: it distinguishes the two fsyncs by what they were actually
    applied to, so replacing the directory one with a second file fsync fails
    here.
    """
    import os
    import stat

    synced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    config_mod.load(tmp_path).save()

    assert stat.S_IFREG in synced, "the config contents were never flushed to disk"
    assert stat.S_IFDIR in synced, "the rename itself was never flushed to disk"


def test_the_rename_is_the_last_thing_that_happens(tmp_path):
    """A reader never sees a partial file, only the old one or the new one.

    The temp file must also not be left behind: `cci doctor` and anything
    else that lists the config directory would be looking at a second,
    stale-looking config.
    """
    cfg = config_mod.load(tmp_path)
    cfg.idle_threshold_s = 120
    cfg.save()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers
    assert config_mod.load(tmp_path).idle_threshold_s == 120


def test_an_unreadable_config_is_never_replaced_with_a_new_host_id(tmp_path):
    """The failure the whole file is arranged around.

    Replacing a config that will not parse means a new host_id, and a new
    host_id forks the history into a duplicate set of rows nothing can merge
    back. So the file is left byte-for-byte as it was, and the command dies
    saying which file and why.
    """
    original = config_mod.load(tmp_path)
    assert original.host_id in (tmp_path / "config.toml").read_text()

    (tmp_path / "config.toml").write_text("this is not = = toml\n")
    with pytest.raises(config_mod.ConfigError, match="config.toml"):
        config_mod.load(tmp_path)

    # Nothing written, nothing regenerated.
    assert (tmp_path / "config.toml").read_text() == "this is not = = toml\n"
    assert not (tmp_path / "config.toml.tmp").exists()


def test_a_config_missing_its_host_id_is_an_error_and_not_a_fresh_install(tmp_path):
    """A KeyError traceback names `raw["host_id"]`, not the file to go and fix.

    This is the half-written case that survived the rename: the file parses,
    it simply stops before the key that matters. Inventing one here would be
    the same fork as replacing the file, arrived at by a different route.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text('db_path = "cc-insights.db"\n')

    with pytest.raises(config_mod.ConfigError, match="host_id"):
        config_mod.load(tmp_path)
    assert (tmp_path / "config.toml").read_text() == 'db_path = "cc-insights.db"\n'


def test_a_truncated_config_does_not_silently_open_a_different_database(tmp_path):
    """`db_path` missing is not the same as `db_path` defaulted.

    Falling back to the default would open a *different* database than the
    one this config describes and report it as that machine's history.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text('host_id = "stable-host-id"\n')

    with pytest.raises(config_mod.ConfigError, match="db_path"):
        config_mod.load(tmp_path)


def test_a_config_that_parses_is_still_loaded_normally(tmp_path):
    """The refusals above are worthless if they also stop the ordinary case."""
    written = config_mod.load(tmp_path)
    again = config_mod.load(tmp_path)
    assert again.host_id == written.host_id
    assert again.db_path == written.db_path


def test_the_repair_declines_a_config_it_cannot_load(tmp_path):
    """A convenience must never be the thing that raises.

    `make_db_path_portable` already declined an unparseable config as "not
    ours to repair". A config that parses but is missing `host_id` is the
    same situation arriving one step later, and the repair running first
    must not be what turns a legible `ConfigError` from the real command
    into a traceback out of a fixup nobody asked for.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    text = f'db_path = "{tmp_path / "cc-insights.db"}"\n'
    (tmp_path / "config.toml").write_text(text)

    assert config_mod.make_db_path_portable(tmp_path) is None
    assert (tmp_path / "config.toml").read_text() == text
