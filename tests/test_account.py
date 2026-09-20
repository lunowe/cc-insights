"""A sign-in must not turn a copied config into a copied bearer token."""

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from cc_insights import account, config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Tests must never read the live credential or inherit a real token."""
    monkeypatch.setenv("CC_INSIGHTS_HOME", str(tmp_path))
    monkeypatch.delenv("CC_INSIGHTS_TOKEN", raising=False)


@pytest.fixture
def credential():
    return account.Credential("private-bearer-token", "acct-alice",
                              "https://insights.example", 1_789_000_000_123)


def test_credential_round_trips(credential, tmp_path):
    """Losing account, server or issue time makes the next sign-in ambiguous."""
    path = account.save(credential, tmp_path)
    assert path == tmp_path / "credentials.toml"
    assert account.load(tmp_path) == credential
    assert account.is_signed_in(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_is_private_from_creation(credential, tmp_path, monkeypatch):
    """Checking only the final mode misses a token exposed before chmod."""
    original_fdopen = os.fdopen
    modes = []

    def checked_fdopen(fd, *args, **kwargs):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        assert os.fstat(fd).st_size == 0
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(account.os, "fdopen", checked_fdopen)
    old_umask = os.umask(0)
    try:
        path = account.save(credential, tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        account.save(replace(credential, token="replacement"), tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)
    assert modes == [0o600, 0o600]
    assert account.load(tmp_path).token == "replacement"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["credentials.toml"]


def test_config_never_receives_the_token(credential, tmp_path):
    """People copy config.toml; doing so must not grant access to their account."""
    cfg = config.load(tmp_path)
    before = cfg.path.read_bytes()
    account.save(credential, tmp_path)
    assert cfg.path.read_bytes() == before
    cfg.save()
    assert credential.token not in cfg.path.read_text()
    assert config.load(tmp_path).host_id == cfg.host_id


def test_missing_file_means_signed_out(tmp_path):
    """Local-only installs must work without manufacturing credentials."""
    assert account.load(tmp_path) is None
    assert not account.is_signed_in(tmp_path)
    assert not (tmp_path / "credentials.toml").exists()


@pytest.mark.parametrize("content", [
    b"this is not = = toml", b"\xff", b"", b'token = "only-a-token"',
    b'token = 42\naccount_id = "a"\nserver_url = "s"\nissued_at = 123',
    b'token = "t"\naccount_id = "a"\nserver_url = "s"\nissued_at = true',
    b'token = "t"\naccount_id = "a"\nserver_url = "s"\nissued_at = "123"',
    b'token = ""\naccount_id = "a"\nserver_url = "s"\nissued_at = 123',
])
def test_broken_credentials_mean_signed_out(content, tmp_path):
    """Malformed bytes, TOML or fields must not crash a local-only install."""
    (tmp_path / "credentials.toml").write_bytes(content)
    assert account.load(tmp_path) is None
    assert not account.is_signed_in(tmp_path)


def test_token_precedence(credential, tmp_path, monkeypatch):
    """A stale saved token must not defeat a deliberate environment override."""
    account.save(credential, tmp_path)
    assert account.token_for(account.load(tmp_path)) == credential.token
    monkeypatch.setenv("CC_INSIGHTS_TOKEN", "environment-token")
    assert account.token_for(account.load(tmp_path)) == "environment-token"
    assert account.token_for(account.load(tmp_path), "flag-token") == "flag-token"
    assert account.load(tmp_path) == credential
    assert "environment-token" not in (tmp_path / "credentials.toml").read_text()
    monkeypatch.setenv("CC_INSIGHTS_TOKEN", "")
    assert account.token_for(account.load(tmp_path)) == credential.token


def test_environment_token_needs_no_file(tmp_path, monkeypatch):
    """Headless callers must be able to supply a token without writing it down."""
    monkeypatch.setenv("CC_INSIGHTS_TOKEN", "environment-token")
    assert account.load(tmp_path) is None
    assert account.token_for(None) == "environment-token"
    assert account.is_signed_in(tmp_path)
    assert not (tmp_path / "credentials.toml").exists()


def test_clear_removes_the_stored_credential(credential, tmp_path):
    """Signing out must remove the secret, not merely hide it from load()."""
    path = account.save(credential, tmp_path)
    account.clear(tmp_path)
    assert not path.exists()
    assert not account.is_signed_in(tmp_path)
    account.clear(tmp_path)


def test_clear_does_not_change_the_environment(credential, tmp_path, monkeypatch):
    """Removing a file cannot revoke a token supplied by the parent process."""
    account.save(credential, tmp_path)
    monkeypatch.setenv("CC_INSIGHTS_TOKEN", "environment-token")
    account.clear(tmp_path)
    assert account.load(tmp_path) is None
    assert account.is_signed_in(tmp_path)


def test_toml_hostile_token_round_trips(credential, tmp_path):
    """Backslashes and quotes must not corrupt the next process's sign-in."""
    hostile = replace(credential, token='prefix\\Users\\alice\\"quoted"\\tail')
    account.save(hostile, tmp_path)
    assert account.load(tmp_path) == hostile


def test_home_override_and_explicit_directory(credential, tmp_path):
    """A chosen config directory must not read or write another install's token."""
    assert account.save(credential) == tmp_path / "credentials.toml"
    assert account.load() == credential
    elsewhere = tmp_path / "other" / "config"
    assert account.load(elsewhere) is None
    second = replace(credential, token="second-token")
    account.save(second, elsewhere)
    assert account.load(elsewhere) == second
    account.clear(elsewhere)
    assert account.load() == credential


def test_failed_replace_preserves_the_old_credential(credential, tmp_path, monkeypatch):
    """An interrupted save must neither destroy sign-in nor strand another token."""
    path = account.save(credential, tmp_path)

    def fail_replace(self, target):
        raise OSError("simulated failed rename")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated failed rename"):
        account.save(replace(credential, token="new-token"), tmp_path)
    assert account.load(tmp_path) == credential
    assert list(tmp_path.iterdir()) == [path]


def test_repr_does_not_disclose_the_token(credential):
    """Logging the credential model must not log the bearer secret with it."""
    assert credential.token not in repr(credential)
