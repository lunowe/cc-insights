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
    assert set(cfg.source_globs) == {"claude_code", "codex"}
    assert cfg.globs_for("codex") and all(not str(p).startswith("~") for p in cfg.globs_for("codex"))
