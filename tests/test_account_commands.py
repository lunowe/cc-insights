"""`cci login/logout/publish/team`, `cci sync auto`, and doctor's account checks.

The transport is proved against a real server in `test_remote_e2e.py`. What
is proved here is the behaviour around it: which backend a command picks,
what it prints before it sends anything, and -- the one that matters most --
that a background job which cannot reach the server still exits 0.
"""

from __future__ import annotations


import pytest

from cc_insights import account, cli, config as config_mod, db, doctor, remote

#: Nothing listens here. Used wherever the test needs a genuine connection
#: failure rather than a mock of one.
DEAD_SERVER = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch the live config: it has a 114 MB database and a real job."""
    monkeypatch.setenv("CC_INSIGHTS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CC_INSIGHTS_TOKEN", raising=False)
    monkeypatch.delenv("CC_INSIGHTS_SYNC_URL", raising=False)
    monkeypatch.delenv("CC_INSIGHTS_SERVER", raising=False)


@pytest.fixture
def home(tmp_path):
    """An initialised config directory with a real, migrated database."""
    where = tmp_path / "home"
    assert cli.main(["--config-dir", str(where), "init"]) == 0
    return where


def signed_in(home, server: str = "https://acct.test") -> account.Credential:
    credential = account.Credential("ccis_test-token", "acc_1", server, 1_700_000_000_000)
    account.save(credential, home)
    return credential


class FakeClient:
    """Stands in for `remote.Client` where the point is not the transport."""

    def __init__(self, *_a, whoami=None, raises=None, **_kw):
        self._whoami = whoami or {"accountId": "acc_1", "actor": "alice", "teams": []}
        self._raises = raises

    def whoami(self):
        if self._raises is not None:
            raise self._raises
        return self._whoami

    def logout(self):
        if self._raises is not None:
            raise self._raises


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _check(checks, name):
    found = [c for c in checks if c.name == name]
    assert found, f"no {name!r} check in {[c.name for c in checks]}"
    return found[0]


def test_not_signed_in_is_ok_and_makes_no_request(home, monkeypatch):
    """Local-first is the default and the tool is complete without an
    account. Warning about it would nag every single-machine user forever."""
    def explode(*_a, **_kw):
        raise AssertionError("doctor must not call the network when signed out")
    monkeypatch.setattr(doctor.remote, "Client", explode)

    check = _check(doctor.run(config_mod.load(home, create=False)), "account")
    assert check.level == doctor.OK
    assert "not signed in" in check.detail


def test_a_rejected_credential_fails_and_names_cci_login(home, monkeypatch):
    """The one account failure a user can act on. Every non-OK check in this
    file names the command that fixes it, and this one has to."""
    signed_in(home)
    monkeypatch.setattr(doctor.remote, "Client",
                        lambda *a, **k: FakeClient(raises=remote.AuthRequired("no")))

    check = _check(doctor.run(config_mod.load(home, create=False)), "account")
    assert check.level == doctor.FAIL
    assert check.fix == "cci login"


def test_an_unreachable_server_warns_rather_than_failing(home, monkeypatch):
    """A server being down costs nothing locally. A red FAIL would send
    somebody looking for a broken install when the answer is "try later"."""
    signed_in(home)
    monkeypatch.setattr(
        doctor.remote, "Client",
        lambda *a, **k: FakeClient(raises=remote.Unreachable("down", code="network")))

    checks = doctor.run(config_mod.load(home, create=False))
    check = _check(checks, "account")
    assert check.level == doctor.WARN
    assert "capture is unaffected" in check.detail
    # Narrowly: no account-side check may be FAIL. `worst()` over the whole
    # run is the wrong assertion here -- this fixture has never ingested, so
    # something unrelated is legitimately failing.
    account_side = [c for c in checks if c.name in {"account", "last push", "auto-push"}]
    assert all(c.level != doctor.FAIL for c in account_side), (
        "a network outage must never make `cci doctor` exit non-zero"
    )


def test_a_never_pushed_machine_is_told_to_push(home, monkeypatch):
    """The product ask fails silently otherwise: the second machine simply
    never sees this one, and nothing anywhere says so."""
    signed_in(home)
    monkeypatch.setattr(doctor.remote, "Client", lambda *a, **k: FakeClient())

    check = _check(doctor.run(config_mod.load(home, create=False)), "last push")
    assert check.level == doctor.WARN
    assert "never" in check.detail
    assert check.fix == "cci sync push"


def test_a_recent_push_is_ok(home, monkeypatch):
    signed_in(home)
    monkeypatch.setattr(doctor.remote, "Client", lambda *a, **k: FakeClient())
    remote.record_publish(home, "https://acct.test", "acc_1")
    state = remote.load_state(home)
    key = remote.target_key("https://acct.test", "acc_1")
    state["targets"][key].setdefault("push", {})["lastSuccessAt"] = remote._now_ms()
    remote.save_state(home, state)

    check = _check(doctor.run(config_mod.load(home, create=False)), "last push")
    assert check.level == doctor.OK


def test_doctor_never_crashes_when_the_account_check_explodes(home, monkeypatch):
    """Doctor is the command somebody runs when things are already wrong. It
    is the one command that must not be the thing that breaks."""
    signed_in(home)

    def boom(*_a, **_kw):
        raise RuntimeError("something nobody anticipated")
    monkeypatch.setattr(doctor.remote, "Client", boom)

    checks = doctor.run(config_mod.load(home, create=False))
    assert _check(checks, "account").level == doctor.WARN


def test_doctor_survives_a_config_with_no_database(tmp_path, monkeypatch):
    """A half-made install must still get an answer, not a traceback."""
    cfg = config_mod.Config(host_id="h", hostname="x",
                            db_path=tmp_path / "nope.db", config_dir=tmp_path)
    monkeypatch.setattr(doctor.remote, "Client", lambda *a, **k: FakeClient())
    checks = doctor.run(cfg)
    assert checks and any(c.level == doctor.FAIL for c in checks)


# --------------------------------------------------------------------------
# cci sync auto -- the background job's step
# --------------------------------------------------------------------------


def test_auto_push_is_a_no_op_when_not_signed_in(home, capsys):
    """It runs on every machine every 15 minutes, including the ones that
    have never heard of an account."""
    assert cli.main(["--config-dir", str(home), "sync", "auto"]) == 0
    assert capsys.readouterr().out == ""


def test_auto_push_exits_zero_when_the_server_is_unreachable(home, capsys):
    """The whole safety argument. A non-zero exit here makes launchd report
    the run as failed and hides a real ingest failure behind a network one --
    and only one of those two actually loses history."""
    signed_in(home, DEAD_SERVER)
    assert cli.main(["--config-dir", str(home), "sync", "auto"]) == 0
    assert "auto-push skipped" in capsys.readouterr().err


def test_auto_push_leaves_the_local_database_untouched_when_it_fails(home):
    """Ingest must succeed whether or not the server does, so a failed push
    must not roll anything back or leave a transaction open."""
    signed_in(home, DEAD_SERVER)
    cfg = config_mod.load(home, create=False)
    conn = db.connect(cfg.db_path)
    before = conn.execute("SELECT count(*) FROM host").fetchone()[0]
    conn.close()

    assert cli.main(["--config-dir", str(home), "sync", "auto"]) == 0

    conn = db.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM host").fetchone()[0] == before
        conn.execute("INSERT INTO host (host_id, hostname, os, first_seen, last_seen) "
                     "VALUES ('h2', 'x', 'y', 1, 2)")
    finally:
        conn.close()


def test_the_job_line_runs_push_last(tmp_path):
    """Ordering is the safety argument: `init` first so a migration cannot
    stop capture, push last so a network failure cannot."""
    from cc_insights import assets

    template = assets.job_template("com.cc-insights.plist")
    command = template.split("<string>__CCI__ init")[1].split("</string>")[0]
    assert command.index("ingest") < command.index("derive") < command.index("sync auto")


# --------------------------------------------------------------------------
# backend selection
# --------------------------------------------------------------------------


def _args(**kw):
    import argparse
    return argparse.Namespace(**{"url": None, "account": False, "direct": False, **kw})


def test_an_explicit_sync_url_wins_over_being_signed_in(home, monkeypatch):
    """Setting `sync_url` is a deliberate act and docs/ACCOUNTS.md §3 keeps
    that mode supported. Signing in is also needed for `cci publish`, so on
    its own it says nothing about where sync should go."""
    signed_in(home)
    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql:///mine")
    cfg = config_mod.load(home, create=False)
    assert cli._backend(_args(), cfg) == cli.DIRECT


def test_being_signed_in_is_enough_with_no_sync_url(home):
    signed_in(home)
    cfg = config_mod.load(home, create=False)
    assert cli._backend(_args(), cfg) == cli.ACCOUNT


def test_the_flags_override_both(home, monkeypatch):
    signed_in(home)
    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql:///mine")
    cfg = config_mod.load(home, create=False)
    assert cli._backend(_args(account=True), cfg) == cli.ACCOUNT
    assert cli._backend(_args(direct=True), cfg) == cli.DIRECT
    assert cli._backend(_args(url="postgresql:///other"), cfg) == cli.DIRECT


def test_nothing_configured_names_every_way_in(home, capsys):
    """The first-time failure. Naming only one way in is how somebody
    concludes the feature needs a database they do not want to run."""
    with pytest.raises(SystemExit):
        cli.main(["--config-dir", str(home), "sync", "push"])
    err = capsys.readouterr().err
    for way in ("cci login", "--url", "CC_INSIGHTS_SYNC_URL", "sync_url"):
        assert way in err


def test_push_says_which_backend_it_used(home, monkeypatch, capsys):
    """Two transports that look identical in the output is how somebody
    spends an afternoon wondering why the other machine is empty."""
    signed_in(home)
    monkeypatch.setattr(cli.remote, "push",
                        lambda *a, **k: remote.TransferStats(direction="push"))
    monkeypatch.setattr(cli.remote, "Client", lambda *a, **k: FakeClient())
    assert cli.main(["--config-dir", str(home), "sync", "push"]) == 0
    assert "your account at https://acct.test" in capsys.readouterr().out


# --------------------------------------------------------------------------
# login and logout
# --------------------------------------------------------------------------


def test_login_needs_to_be_told_which_server(home, capsys):
    """There is no default instance and inventing one would point somebody's
    filesystem paths at an address nobody chose."""
    assert cli.main(["--config-dir", str(home), "login"]) == 1
    assert "--server" in capsys.readouterr().err


def test_login_prints_the_user_code_and_stores_the_credential(home, monkeypatch, capsys):
    """If the device code were printed instead, somebody would type a
    bearer-equivalent secret into a web page."""
    flow = remote.DeviceFlow("dev_SECRET", "WDJB-MJHT", "https://gh/device",
                             "https://gh/device?user_code=WDJB-MJHT", 9e12, 5)

    class Stub(FakeClient):
        def device_start(self, _name):
            return flow

        def device_await(self, _flow, **_kw):
            return remote.Identity("ccis_new", "acc_1", "alice", None)

    monkeypatch.setattr(cli.remote, "Client", lambda *a, **k: Stub())
    assert cli.main(["--config-dir", str(home), "login",
                     "--server", "https://acct.test"]) == 0

    out = capsys.readouterr().out
    assert "WDJB-MJHT" in out and "https://gh/device" in out
    assert "dev_SECRET" not in out, "the device code is not for a person to read"
    assert "signed in as alice" in out

    stored = account.load(home)
    assert stored is not None
    assert stored.token == "ccis_new" and stored.server_url == "https://acct.test"


def test_login_claims_this_host_without_changing_its_id(home, monkeypatch):
    """`host_id` is baked into every session id. Reassigning it on sign-in
    would fork the entire history into a duplicate set of rows."""
    cfg = config_mod.load(home, create=False)
    original = cfg.host_id

    class Stub(FakeClient):
        def device_start(self, _name):
            return remote.DeviceFlow("d", "C", "u", "u", 9e12, 5)

        def device_await(self, _flow, **_kw):
            return remote.Identity("ccis_new", "acc_XYZ", "alice", None)

    monkeypatch.setattr(cli.remote, "Client", lambda *a, **k: Stub())
    assert cli.main(["--config-dir", str(home), "login",
                     "--server", "https://acct.test"]) == 0

    assert config_mod.load(home, create=False).host_id == original
    conn = db.connect(cfg.db_path)
    try:
        row = conn.execute("SELECT host_id, account_id FROM host").fetchone()
    finally:
        conn.close()
    assert row[0] == original
    assert row[1] == "acc_XYZ"


def test_logout_clears_the_credential_even_when_the_server_is_down(home, capsys):
    """Signing out of a laptop you are about to lose must not require the
    network to agree."""
    signed_in(home, DEAD_SERVER)
    assert cli.main(["--config-dir", str(home), "logout"]) == 0
    assert account.load(home) is None
    out = capsys.readouterr().out
    assert "not revoked" in out
    assert "stays valid until revoked" in out


def test_logout_when_not_signed_in_says_so(home, capsys):
    assert cli.main(["--config-dir", str(home), "logout"]) == 0
    assert "nothing to clear" in capsys.readouterr().out


def test_logout_does_not_claim_to_clear_an_environment_token(home, monkeypatch, capsys):
    """`CC_INSIGHTS_TOKEN` is not ours to remove, and reporting a sign-out
    that did not happen is worse than saying nothing."""
    monkeypatch.setenv("CC_INSIGHTS_TOKEN", "ccis_from-the-env")
    assert cli.main(["--config-dir", str(home), "logout"]) == 0
    assert "still applies" in capsys.readouterr().out
