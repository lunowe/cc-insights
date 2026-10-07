"""`GET /join/{code}`: the page a join link shows in a browser.

It must say how to use the link, and nothing about the code: a live code, a
spent one and an invented one get the same page, or the page becomes an
oracle for which teams exist.
"""

from __future__ import annotations

from cci_server import invites

FAKE = invites.INVITE_PREFIX + "x" * 43


def test_the_page_names_the_join_command_with_the_public_address(client):
    r = client.get(f"/join/{FAKE}", headers={"x-forwarded-proto": "https",
                                             "host": "cci.example.org"})
    assert r.status_code == 200
    assert f"cci team join https://cci.example.org/join/{FAKE}" in r.text
    assert f"--join https://cci.example.org/join/{FAKE}" in r.text


def test_a_live_code_and_an_invented_one_get_the_same_page(client, make_account):
    admin = make_account("alice")
    team = client.post("/v1/teams", json={"name": "Platform"}, headers=admin.auth).json()
    live = client.post(f"/v1/teams/{team['teamId']}/invites", json={},
                       headers=admin.auth).json()["code"]

    real = client.get(f"/join/{live}").text.replace(live, "CODE")
    invented = client.get(f"/join/{FAKE}").text.replace(FAKE, "CODE")
    assert real == invented


def test_the_secret_address_is_not_cached_indexed_or_referred(client):
    r = client.get(f"/join/{FAKE}")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in r.headers["x-robots-tag"]


def test_anything_not_shaped_like_a_code_is_never_echoed(client):
    r = client.get("/join/<script>alert(1)</script>")
    assert r.status_code == 404
    assert "<script>" not in r.text


def test_a_hostile_host_header_is_escaped(client):
    r = client.get(f"/join/{FAKE}", headers={"host": "x\"><script>alert(1)</script>"})
    assert "<script>alert" not in r.text


def test_healthz_reports_the_schema_not_what_this_boot_applied(settings, migrated_db, provider):
    """The image migrates before it serves, so the app's own migrate applies
    nothing. /healthz must still list every applied migration."""
    from fastapi.testclient import TestClient

    from cci_server.app import create_app

    app = create_app(settings, db=migrated_db, provider=provider, migrate=True)
    with TestClient(app) as c:
        listed = c.get("/healthz").json()["migrations"]
    assert listed and listed == sorted(listed) and listed[0] == 1
