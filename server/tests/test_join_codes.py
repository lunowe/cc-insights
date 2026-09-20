"""Join codes, written as attacks on the credential.

A join code is a bearer secret that grants access to other people's agent
time. It is not a password and it is not an API token, but it is closer to
both than it looks, and the failure mode is the one this whole project exists
to avoid: somebody reads work that was never shared with them.

Each test here is one property from the threat model, written so that it fails
if that property is removed. The properties, and where they live:

  1. hashed at rest, plaintext never stored   -- migration 005, `invites.py`
  2. >= 128 bits of entropy                   -- `invites.CODE_BYTES`
  3. expiring, revocable, use-limited         -- `invites.redeem`
  4. shown exactly once                       -- `routes/teams.create_invite`
  5. never in an error body or a log          -- `routes/teams.join_team`
  6. redemption requires authentication       -- `Depends(principal)`
  7. who minted and who redeemed, recorded    -- `team_invite_redemption`

`test_invite_disclosure.py` is the other half and asks the harder question:
whether joining a team can be made to WIDEN anything.
"""

from __future__ import annotations

import time

import pytest
from conftest import join_team

from cci_server import invites, tokens

WELL_FORMED_BUT_WRONG = invites.INVITE_PREFIX + "l3Ct1rkBHOmvnBaMDbG9yrYOHT5-a_kXTJrPgQVsf1c"


def make_team(client, who, name="Platform") -> str:
    r = client.post("/v1/teams", json={"name": name}, headers=who.auth)
    assert r.status_code == 201, r.text
    return r.json()["teamId"]


def mint(client, admin, team_id, **body) -> dict:
    r = client.post(f"/v1/teams/{team_id}/invites", json=body, headers=admin.auth)
    assert r.status_code == 201, r.text
    return r.json()


def redeem(client, who, code: str):
    return client.post("/v1/teams/join", json={"code": code}, headers=who.auth)


# --------------------------------------------------------------------------
# 1. hashed at rest
# --------------------------------------------------------------------------


def _every_text_value(conn) -> list[tuple[str, str, str]]:
    """(table, column, value) for every text-ish cell in the database.

    Enumerated from `information_schema` rather than from a list, for the
    reason `team_tables` gives: a hardcoded list cannot fail to mention a
    table it has never heard of, so the one place a leaked code would land --
    a column somebody added next year -- is the one place a list would not
    look.
    """
    tables = [
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
    ]
    out: list[tuple[str, str, str]] = []
    for table in tables:
        cols = [
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "AND data_type IN ('text','character varying','character')",
                (table,),
            )
        ]
        for col in cols:
            for r in conn.execute(
                f'SELECT "{col}" AS v FROM "{table}" WHERE "{col}" IS NOT NULL'
            ):
                out.append((table, col, r["v"]))
    return out


def test_the_plaintext_of_a_join_code_is_nowhere_in_the_database(
    client, alice, bob, migrated_db
):
    """The attack this defends against is a database dump, not a live server.

    A backup, a `psql` session, a replica somebody forgot to lock down, a
    screenshot of a support query. If the plaintext is in any of them, every
    live invite on the instance is usable by whoever is reading -- and unlike
    a stolen bearer token, an invite grants membership of a team the reader
    was never meant to be near.

    `api_token` already holds its secrets hashed for exactly this reason. The
    test sweeps EVERY text column in the schema rather than just
    `team_invite.code_hash`, because the way this property actually dies is
    somebody adding a `last_code` convenience column or logging the plaintext
    into an audit table, not somebody deleting the hash.
    """
    team = make_team(client, alice)
    minted = mint(client, alice, team, note="for the contractors")
    code = minted["code"]
    # Redeem it too: a code that is never used exercises none of the paths
    # that might store it on the way through.
    assert redeem(client, bob, code).status_code == 200

    with migrated_db.connection() as conn:
        cells = _every_text_value(conn)
        offenders = [(t, c) for t, c, v in cells if code in v]
        assert not offenders, f"the join code plaintext is stored in {offenders}"

        # And the hash IS there, so the test above cannot pass by the invite
        # never having been written at all.
        digest = invites.hash_code(code)
        assert any(v == digest for _, _, v in cells), "the invite was not stored"


# --------------------------------------------------------------------------
# 2. entropy
# --------------------------------------------------------------------------


def test_a_join_code_carries_at_least_128_bits_of_entropy(client, alice):
    """There is no attempt throttle on this server, so entropy is the defence.

    That is the whole argument and it is worth stating as an attack: an
    attacker who knows a team exists can POST `/v1/teams/join` as fast as the
    network allows, with any account they can create, and nothing on this
    server will slow them down or lock them out. The only thing standing
    between them and somebody else's roster is how many codes they would have
    to try.

    Hence long and opaque rather than short and typeable. The device flow's
    `43CA-9AAA` is ~40 bits and correct THERE -- it is bound to one in-flight
    grant, it lives for minutes, and GitHub throttles the far side. A join
    code is pasted into Slack and lives for days, so it gets the full 256
    bits from `secrets.token_urlsafe(32)`.
    """
    team = make_team(client, alice)
    codes = {mint(client, alice, team)["code"] for _ in range(24)}
    assert len(codes) == 24, "two mints collided, which 256 bits does not do"

    for code in codes:
        assert code.startswith(invites.INVITE_PREFIX)
        body = code[len(invites.INVITE_PREFIX):]
        # url-safe base64 of 32 bytes is 43 characters with no padding. Each
        # character carries 6 bits, so 43 * 6 = 258 >= 256 >= 128.
        assert len(body) >= 22, (
            f"{len(body)} url-safe base64 characters is {len(body) * 6} bits; "
            "the threat model requires at least 128 and there is no throttle"
        )
        assert len(body) == 43, "not the 256 bits invites.CODE_BYTES promises"

    # Not merely long: actually varying. A constant suffix would pass the
    # length check above while carrying no entropy at all.
    bodies = [c[len(invites.INVITE_PREFIX):] for c in codes]
    for position in range(43):
        assert len({b[position] for b in bodies}) > 1, (
            f"character {position} is the same in all 24 codes"
        )


def test_a_join_code_is_compared_in_constant_time(monkeypatch, client, alice, bob):
    """`==` on a digest leaks, through timing, how much of it matched.

    Not a practical attack against a sha256 of a 256-bit secret, and that is
    not the reason this is pinned. The reason is that "we compare hashes with
    `==` here but not there" is a distinction nobody can hold in their head,
    and the next person to add a lookup path will copy whatever they find.
    `tokens.py` established constant-time comparison for this project's other
    bearer secret; this keeps the two the same.

    Fails if `invites.matches` becomes `stored == hash_code(presented)`.
    """
    calls: list[tuple[str, str]] = []
    real = tokens.constant_time_eq

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tokens, "constant_time_eq", spy)

    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]
    assert redeem(client, bob, code).status_code == 200
    assert calls, "the redemption path never reached a constant-time comparison"

    # And it compares the right things: a digest against a digest, never the
    # plaintext against anything.
    assert all(code not in a and code not in b for a, b in calls), \
        "the plaintext code was passed to the comparison"

    # A code one character different is rejected.
    mangled = code[:-1] + ("A" if code[-1] != "A" else "B")
    assert not invites.matches(invites.hash_code(code), mangled)


# --------------------------------------------------------------------------
# 3. expiring, revocable, use-limited -- and the defaults
# --------------------------------------------------------------------------


def test_the_defaults_are_single_use_and_short_lived(client, alice):
    """An admin who passes nothing gets the safe code, not the convenient one.

    Both halves matter and for different reasons. Single-use bounds the blast
    radius when the channel the code was pasted into turns out to be wider
    than the admin thought. A deadline matters because the code will sit in
    that channel forever and nobody revokes a code they have forgotten they
    minted -- so the expiry is the control that works without anybody
    remembering anything.
    """
    team = make_team(client, alice)
    minted = mint(client, alice, team)
    assert minted["maxUses"] == 1
    assert minted["role"] == "member", "an unasked-for code must not confer admin"
    lifetime = minted["expiresAt"] - minted["createdAt"]
    assert lifetime == invites.DEFAULT_TTL_MS
    assert lifetime <= 7 * 24 * 3_600_000, "a default this long is a forgotten code"


def test_an_expired_code_cannot_be_redeemed(client, alice, bob):
    team = make_team(client, alice)
    code = mint(client, alice, team, expiresInMs=1)["code"]
    time.sleep(0.01)
    assert redeem(client, bob, code).status_code == 404
    assert client.get("/v1/teams", headers=bob.auth).json()["teams"] == []


def test_a_revoked_code_cannot_be_redeemed(client, alice, bob):
    """Revocation is the control for a code that reached the wrong person.

    The realistic sequence: an admin pastes a code into the wrong Slack
    channel, notices, and needs it dead in seconds. Nothing about the code
    itself can help -- it is already out -- so the only thing that can is the
    server refusing it.
    """
    team = make_team(client, alice)
    minted = mint(client, alice, team)
    dead = client.delete(f"/v1/teams/{team}/invites/{minted['inviteId']}",
                         headers=alice.auth)
    assert dead.status_code == 204

    assert redeem(client, bob, minted["code"]).status_code == 404
    assert client.get("/v1/teams", headers=bob.auth).json()["teams"] == []

    # Idempotent, and no 404 for an id that was never an invite: taking
    # access away is never made harder than granting it.
    assert client.delete(f"/v1/teams/{team}/invites/{minted['inviteId']}",
                         headers=alice.auth).status_code == 204
    assert client.delete(f"/v1/teams/{team}/invites/inv_nope",
                         headers=alice.auth).status_code == 204


def test_a_single_use_code_admits_one_person_and_then_nobody(
    client, alice, bob, make_account
):
    """The seat is spent by the first redeemer. The second is a stranger again.

    This is the property that makes a leaked code survivable: the admin sent
    it to one person, that person used it, and the copy sitting in the chat
    log is now worth nothing.
    """
    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]

    assert redeem(client, bob, code).status_code == 200
    carol = make_account("carol")
    assert redeem(client, carol, code).status_code == 404
    assert client.get("/v1/teams", headers=carol.auth).json()["teams"] == []

    members = client.get(f"/v1/teams/{team}/members", headers=alice.auth).json()
    assert sorted(m["actor"] for m in members["members"]) == ["alice", "bob"]


def test_a_multi_use_code_stops_at_its_limit(client, alice, make_account):
    team = make_team(client, alice)
    code = mint(client, alice, team, maxUses=2)["code"]
    for actor in ("bob", "carol"):
        assert redeem(client, make_account(actor), code).status_code == 200
    assert redeem(client, make_account("dave"), code).status_code == 404


def test_the_lifetime_and_the_use_count_are_capped(client, alice):
    """An admin cannot mint an immortal code, however hard they ask.

    Clamped rather than refused -- a 400 teaches people to fight the limit --
    but the response says what was actually minted, so nobody promises a
    colleague a code that will still work next quarter.
    """
    team = make_team(client, alice)
    greedy = mint(client, alice, team, expiresInMs=10 * 365 * 24 * 3_600_000,
                  maxUses=100_000)
    assert greedy["expiresAt"] - greedy["createdAt"] == invites.MAX_TTL_MS
    assert greedy["maxUses"] == invites.MAX_USES


def test_a_retried_redemption_does_not_spend_a_second_seat(client, alice, bob):
    """A flaky connection must not cost the team a seat.

    `cci team join` run twice -- or once, with the response lost on the way
    back -- is the ordinary case, not an attack. The second call reports what
    is already true and consumes nothing.
    """
    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]
    assert redeem(client, bob, code).json()["alreadyMember"] is False
    again = redeem(client, bob, code)
    assert again.status_code == 200
    assert again.json()["alreadyMember"] is True

    listed = client.get(f"/v1/teams/{team}/invites", headers=alice.auth).json()
    assert listed["invites"][0]["uses"] == 1, "a retry burned a seat"


def test_redeeming_an_admin_code_does_not_promote_an_existing_member(
    client, alice, bob
):
    """Otherwise a leaked admin code is a self-promotion route for insiders.

    Everybody already on the team can see that an admin code exists the
    moment one is pasted anywhere near them. If redemption upgraded an
    existing membership, a member who got hold of one would make themselves
    an admin -- and an admin can roster repos, which is the one action in
    this system that widens what other people see.

    Promotion stays an admin action on an admin route, about a named person.
    """
    team = make_team(client, alice)
    join_team(client, alice, team, bob, role="member")

    admin_code = mint(client, alice, team, role="admin")["code"]
    out = redeem(client, bob, admin_code)
    assert out.status_code == 200
    assert out.json()["role"] == "member", "an existing member self-promoted"

    members = {m["actor"]: m for m in
               client.get(f"/v1/teams/{team}/members", headers=alice.auth).json()["members"]}
    assert members["bob"]["role"] == "member"
    # And Bob still cannot use an admin route.
    assert client.post(f"/v1/teams/{team}/invites", json={},
                       headers=bob.auth).status_code == 403


def test_rejoining_after_removal_spends_another_seat(client, alice, bob):
    """Removal has to be effective, so an old code is not a standing back door.

    An admin who removes somebody expects them to be out. If a spent
    single-use code let its original redeemer walk back in, removal would
    quietly depend on the admin also remembering to revoke every code that
    person ever used -- and they will not.
    """
    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]
    assert redeem(client, bob, code).status_code == 200
    assert client.delete(f"/v1/teams/{team}/members/{bob.account_id}",
                         headers=alice.auth).status_code == 204

    assert redeem(client, bob, code).status_code == 404
    assert client.get("/v1/teams", headers=bob.auth).json()["teams"] == []


# --------------------------------------------------------------------------
# 4. shown exactly once
# --------------------------------------------------------------------------


def test_the_code_is_in_the_minting_response_and_in_no_other(client, alice, bob):
    """There is no way to recover a code, and that is a property not a gap.

    An endpoint that could re-show one would turn a single compromised admin
    session into a way to recover every live invite on the instance. Because
    only the sha256 was kept, that endpoint cannot be written -- which is why
    `cci team invite` tells the admin to copy it now, on the same screen.
    """
    team = make_team(client, alice)
    minted = mint(client, alice, team, note="for the contractors")
    code = minted["code"]

    listed = client.get(f"/v1/teams/{team}/invites", headers=alice.auth)
    assert code not in listed.text, "the invite listing re-shows the code"
    assert listed.json()["invites"][0]["inviteId"] == minted["inviteId"]

    # Nor anywhere else an admin or a member can reach.
    for path in (f"/v1/teams/{team}/members", "/v1/teams", "/v1/auth/whoami",
                 "/v1/team/repos", "/v1/team/summary"):
        assert code not in client.get(path, headers=alice.auth).text, path

    assert redeem(client, bob, code).status_code == 200
    for path in (f"/v1/teams/{team}/members", f"/v1/teams/{team}/invites", "/v1/teams"):
        assert code not in client.get(path, headers=alice.auth).text, path


# --------------------------------------------------------------------------
# 5. every failure is the same failure, and none of them echo the code
# --------------------------------------------------------------------------


def test_every_bad_code_gives_exactly_the_same_answer(
    client, alice, bob, make_account
):
    """An invalid code and an expired code must be indistinguishable.

    "That code has expired" confirms the code was real, which confirms the
    team is real, to somebody who has just demonstrated they were not invited
    to it. That is `errors.not_found`'s rule -- a distinguishable refusal
    answers a question the caller was not entitled to ask -- applied to a
    credential instead of a row.

    The defence is worth spelling out because the tempting kindness here is
    considerable: every one of these five cases has a genuinely helpful
    message, and an admin debugging an onboarding problem would like all of
    them. They are the same sentence anyway.
    """
    team = make_team(client, alice)

    expired = mint(client, alice, team, expiresInMs=1)["code"]
    time.sleep(0.01)

    revoked = mint(client, alice, team)
    client.delete(f"/v1/teams/{team}/invites/{revoked['inviteId']}", headers=alice.auth)

    exhausted = mint(client, alice, team)["code"]
    assert redeem(client, make_account("carol"), exhausted).status_code == 200

    answers = {
        name: redeem(client, bob, code)
        for name, code in (
            ("unknown", WELL_FORMED_BUT_WRONG),
            ("expired", expired),
            ("revoked", revoked["code"]),
            ("exhausted", exhausted),
            ("malformed", "not-a-code-at-all"),
        )
    }
    for name, resp in answers.items():
        assert resp.status_code == 404, f"{name} is distinguishable by status"

    bodies = {name: resp.json() for name, resp in answers.items()}
    distinct = {(b["error"], b["message"]) for b in bodies.values()}
    assert len(distinct) == 1, f"five reasons produced {len(distinct)} answers: {bodies}"
    assert bodies["unknown"]["error"] == "not_found"


def test_a_rejected_code_is_never_echoed_back(client, alice, bob):
    """Not in the message, not in a `detail`, not in a validation error.

    An echoed credential lands wherever the response lands: a terminal
    scrollback, a CI log, a bug report someone pastes into an issue. The
    caller already knows what they sent, so there is nothing to be gained by
    repeating it and a live code to lose.
    """
    team = make_team(client, alice)
    live = mint(client, alice, team)["code"]

    for code in (WELL_FORMED_BUT_WRONG, "not-a-code-at-all", live + "x"):
        resp = redeem(client, bob, code)
        assert resp.status_code == 404
        assert code not in resp.text, "the rejected code came back in the body"
        # Nor the live code it was one character away from.
        assert live not in resp.text

    # The team is not named either: a message saying which team the code was
    # "for" would be the existence oracle arriving through the error path.
    assert team not in redeem(client, bob, WELL_FORMED_BUT_WRONG).text


def test_a_bad_code_does_not_reveal_whether_the_team_exists(client, alice, bob):
    """A guesser gets the same 404 whether or not there is anything to find.

    With no team at all on the instance and with a real team they were not
    invited to, the answer has to be identical -- otherwise `POST
    /v1/teams/join` is an oracle for "is anybody using this server", and with
    a partially-guessed code, for "is that team real".
    """
    nothing_exists = redeem(client, bob, WELL_FORMED_BUT_WRONG)
    make_team(client, alice, "A Real Team")
    something_exists = redeem(client, bob, WELL_FORMED_BUT_WRONG)

    assert nothing_exists.status_code == something_exists.status_code == 404
    assert nothing_exists.json() == something_exists.json()


# --------------------------------------------------------------------------
# 6. redemption requires authentication
# --------------------------------------------------------------------------


def test_a_code_alone_cannot_mint_an_identity(client, alice):
    """The code says which team. The bearer token says who is joining.

    If a code alone were enough, it would be a credential that CREATES an
    account -- and whoever found it in a Slack export would be a person on
    the team rather than a stranger holding a string. Worse, there would be
    no consent record, because there would be nobody identified to have
    consented: the property this entire feature exists to establish would be
    gone while the feature still appeared to work.
    """
    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]

    anonymous = client.post("/v1/teams/join", json={"code": code})
    assert anonymous.status_code == 401
    assert anonymous.json()["error"] == "unauthenticated"
    assert code not in anonymous.text

    bad_token = client.post("/v1/teams/join", json={"code": code},
                            headers={"Authorization": "Bearer ccis_nonsense"})
    assert bad_token.status_code == 401

    # And the seat was not spent by either attempt.
    assert client.get(f"/v1/teams/{team}/invites",
                      headers=alice.auth).json()["invites"][0]["uses"] == 0


# --------------------------------------------------------------------------
# 7. the audit trail
# --------------------------------------------------------------------------


def test_who_minted_and_who_redeemed_is_recorded_with_timestamps(
    client, alice, bob, make_account
):
    """"How did they get in" has to be answerable after the fact.

    This is the question an admin asks when something has gone wrong, and it
    has two ends: who opened the door, and who walked through it. One without
    the other is not an answer -- a redemption with no minter does not say
    who is accountable, and a minter with no redemption does not say whether
    anything happened.
    """
    team = make_team(client, alice)
    before = int(time.time() * 1000)
    minted = mint(client, alice, team, maxUses=2, note="design contractors")
    carol = make_account("carol")
    assert redeem(client, bob, minted["code"]).status_code == 200
    assert redeem(client, carol, minted["code"]).status_code == 200
    after = int(time.time() * 1000)

    entry = client.get(f"/v1/teams/{team}/invites",
                       headers=alice.auth).json()["invites"][0]
    assert entry["createdByActor"] == "alice"
    assert entry["note"] == "design contractors"
    assert entry["uses"] == 2 and entry["maxUses"] == 2
    assert entry["active"] is False, "a spent code must not read as usable"
    assert before <= entry["createdAt"] <= after

    redeemed = {r["actor"]: r for r in entry["redemptions"]}
    assert set(redeemed) == {"bob", "carol"}
    for r in redeemed.values():
        assert before <= r["redeemedAt"] <= after
        assert r["accountId"]

    # The membership itself names the person who let them in, and it is shown
    # to every member rather than to admins alone.
    members = {m["actor"]: m for m in
               client.get(f"/v1/teams/{team}/members", headers=bob.auth).json()["members"]}
    assert members["bob"]["invitedByActor"] == "alice"
    assert members["carol"]["invitedByActor"] == "alice"
    # Alice created the team; there was no invite and none is invented.
    assert members["alice"]["invitedByActor"] is None


def test_the_record_survives_the_member_leaving(client, alice, bob):
    """Somebody joining, reading a week of data and leaving is the case.

    That is precisely the sequence an admin most needs to reconstruct, and it
    is the one a membership table alone cannot show -- the row is gone. The
    redemption log is append-only for this reason.
    """
    team = make_team(client, alice)
    code = mint(client, alice, team)["code"]
    assert redeem(client, bob, code).status_code == 200
    assert client.delete(f"/v1/teams/{team}/members/{bob.account_id}",
                         headers=bob.auth).status_code == 204

    entry = client.get(f"/v1/teams/{team}/invites",
                       headers=alice.auth).json()["invites"][0]
    assert [r["actor"] for r in entry["redemptions"]] == ["bob"]


# --------------------------------------------------------------------------
# who may mint, and the route that is gone
# --------------------------------------------------------------------------


def test_the_add_member_by_id_route_is_gone(client, alice, bob, make_account):
    """Nobody can be put on a roster without redeeming a code themselves.

    The old route is not merely discouraged -- it does not answer. A member
    who is on a team by administrative fiat has consented to nothing, and the
    point of this work is that the set of people who can read your agent time
    is a set every one of them opted into.
    """
    team = make_team(client, alice)
    carol = make_account("carol")

    gone = client.post(f"/v1/teams/{team}/members",
                       json={"accountId": carol.account_id, "role": "admin"},
                       headers=alice.auth)
    assert gone.status_code == 405, "the by-id membership route still answers"
    assert client.get("/v1/teams", headers=carol.auth).json()["teams"] == []

    # And the listing route on the same path still works, so the 405 above is
    # the method being gone rather than the path.
    assert client.get(f"/v1/teams/{team}/members", headers=alice.auth).status_code == 200


def test_only_an_admin_can_mint_a_code(client, alice, bob, make_account):
    """A member minting codes is a member deciding who else reads the roster."""
    team = make_team(client, alice)
    join_team(client, alice, team, bob)

    refused = client.post(f"/v1/teams/{team}/invites", json={}, headers=bob.auth)
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_an_admin"
    assert client.get(f"/v1/teams/{team}/invites", headers=bob.auth).status_code == 403


def test_a_stranger_gets_404_rather_than_403_for_a_teams_invites(
    client, alice, make_account
):
    """404, not 403: a distinguishable 403 confirms the team exists.

    `team_id` appears in output people paste into issues, so "is this a real
    team" is a question a stranger holding one must not be able to answer.
    """
    team = make_team(client, alice)
    stranger = make_account("mallory")
    for call in (
        lambda: client.post(f"/v1/teams/{team}/invites", json={}, headers=stranger.auth),
        lambda: client.get(f"/v1/teams/{team}/invites", headers=stranger.auth),
        lambda: client.delete(f"/v1/teams/{team}/invites/inv_x", headers=stranger.auth),
    ):
        r = call()
        assert r.status_code == 404, r.text
        assert r.json()["error"] == "not_found"


def test_an_invalid_role_is_refused_rather_than_silently_downgraded(client, alice):
    team = make_team(client, alice)
    r = client.post(f"/v1/teams/{team}/invites", json={"role": "owner"},
                    headers=alice.auth)
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_role"


# --------------------------------------------------------------------------
# leaving
# --------------------------------------------------------------------------


def test_a_member_can_leave_without_asking_an_admin(client, alice, bob):
    """Consent that cannot be withdrawn is not much of a consent model.

    Joining is now something a person does deliberately; leaving has to be
    too, or being talked into a team once is a permanent arrangement.
    """
    team = make_team(client, alice)
    join_team(client, alice, team, bob)
    assert client.delete(f"/v1/teams/{team}/members/{bob.account_id}",
                         headers=bob.auth).status_code == 204
    assert client.get("/v1/teams", headers=bob.auth).json()["teams"] == []
    # And the team no longer shows them.
    members = client.get(f"/v1/teams/{team}/members", headers=alice.auth).json()
    assert [m["actor"] for m in members["members"]] == ["alice"]


def test_the_last_admin_cannot_leave_either(client, alice, bob):
    """The last-admin rule holds for a departure exactly as for a removal.

    A team whose only admin walks out is a roster nobody can correct, with
    its repos visible to everyone left on it forever. Leaving being a
    self-service action is no reason for it to be the one path that skips the
    check -- which is why it is the same route.
    """
    team = make_team(client, alice)
    join_team(client, alice, team, bob)

    refused = client.delete(f"/v1/teams/{team}/members/{alice.account_id}",
                            headers=alice.auth)
    assert refused.status_code == 409
    assert refused.json()["error"] == "last_admin"

    # With a second admin, she can go.
    promoted = client.post(f"/v1/teams/{team}/invites", json={"role": "admin"},
                           headers=alice.auth).json()
    # Bob is already a member, so the admin code does not promote him -- the
    # admin route is what does, and that is the point of the test above.
    assert redeem(client, bob, promoted["code"]).json()["role"] == "member"


def test_leaving_a_team_you_are_not_on_is_the_same_404_as_one_that_does_not_exist(
    client, alice, make_account
):
    team = make_team(client, alice)
    stranger = make_account("mallory")
    real = client.delete(f"/v1/teams/{team}/members/{stranger.account_id}",
                         headers=stranger.auth)
    fake = client.delete("/v1/teams/tm_nope/members/" + stranger.account_id,
                         headers=stranger.auth)
    assert real.status_code == fake.status_code == 404
    assert real.json() == fake.json()


# --------------------------------------------------------------------------
# the pure helpers, where the clamps live
# --------------------------------------------------------------------------


@pytest.mark.parametrize("asked,expected", [
    (None, invites.DEFAULT_TTL_MS),
    (1, 1),
    (60_000, 60_000),
    (invites.MAX_TTL_MS + 1, invites.MAX_TTL_MS),
    (10**15, invites.MAX_TTL_MS),
])
def test_clamp_ttl_never_returns_an_unbounded_lifetime(asked, expected):
    assert invites.clamp_ttl(asked) == expected


@pytest.mark.parametrize("asked,expected", [
    (None, 1), (1, 1), (7, 7), (invites.MAX_USES + 1, invites.MAX_USES),
])
def test_clamp_uses_never_returns_an_unbounded_seat_count(asked, expected):
    assert invites.clamp_uses(asked) == expected
