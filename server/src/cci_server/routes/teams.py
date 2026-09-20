"""Teams, rosters, and the branch-name switch.

docs/SERVER_API.md §4.5. This file is administration; `team_data.py` is the
data. They are apart because a mistake in one is an inconvenience and a
mistake in the other is a disclosure.

THE RULE THAT IS NOT IN docs/ACCOUNTS.md, and has to be.
Adding a repo to a roster grants a whole team sight of it, so the question is
who may add one. The answer cannot be "any admin, any repo_id", and the reason
is the same property that makes `repo_id` safe to publish:

    repo_id = sha256("repo" + normalized_remote)

docs/REDACTION.md §0 establishes that this is fine BECAUSE the remote is
already public on the far side of the boundary -- there is nothing to confirm.
The flip side is that the id is computable by anyone who can guess the remote.
If a roster accepted an arbitrary id, then knowing that somebody works on
github.com/acme/secret would be enough: hash it, add it to a team of one, and
read their sessions on it.

So an admin may only add a repo that is ALREADY IN THEIR OWN SCOPE. A roster
can widen who sees a repo; it can never widen which repos the person doing the
widening can see. That keeps every path into the team store rooted in the rule
from docs/REDACTION.md §1 -- only to people who can already see that repo.

AND THAT CHECK IS NOT SUFFICIENT ON ITS OWN, which is the part that was
missing. Publishing gives an account sight of its OWN rows in a repo, so a
publisher passes the check above -- and must, because a first publisher
rostering their own repo is how a team gets a repo at all on an instance with
no GitHub app (docs/SERVER_API.md §4.6 requires that to keep working). At the
moment of the call, a legitimate first publisher and somebody who guessed the
remote and published one row under it are indistinguishable. Nothing here can
tell them apart, and no check placed here ever will.

So the grant is DELEGATED rather than absolute, and `scope.resolve` is where
that happens: a roster hands the team what the adder had. Forge-verified for
the repo, and the team sees every row in it; otherwise the team sees the
adder's own rows and nobody else's. Both people above then get exactly what
they were entitled to share, and neither of them gets more. `added_by` is
what carries it, which is why this handler keeps it current.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from cci_server import ids, scope as scope_mod, tokens
from cci_server.auth import get_conn, principal
from cci_server.db import now_ms
from cci_server.errors import conflict, forbidden, not_found

router = APIRouter(prefix="/v1/teams", tags=["teams"])

ADMIN = "admin"
MEMBER = "member"


class CreateTeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AddMemberBody(BaseModel):
    accountId: str
    role: str = MEMBER


class AddRepoBody(BaseModel):
    repoId: str
    branchNamesPublished: bool = True


class BranchSwitchBody(BaseModel):
    branchNamesPublished: bool


def _role(conn, team_id: str, account_id: str) -> str:
    """This caller's role on this team, or 404.

    404 and not 403 for a team the caller is not on. A distinguishable 403
    would confirm the team exists, and team ids appear in `whoami` output that
    people paste into issues.
    """
    row = conn.execute(
        "SELECT role FROM team_member WHERE team_id = %s AND account_id = %s",
        (team_id, account_id),
    ).fetchone()
    if row is None:
        raise not_found("No such team.")
    return row["role"]


def _require_admin(conn, team_id: str, account_id: str) -> None:
    if _role(conn, team_id, account_id) != ADMIN:
        # 403 here, deliberately: the caller is a member, so they already know
        # the team exists and nothing is disclosed by being specific.
        raise forbidden("not_an_admin", "Only a team admin can do that.")


@router.get("")
def list_teams(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    rows = conn.execute(
        """SELECT t.team_id, t.name, t.created_at, tm.role
           FROM team_member tm JOIN team t ON t.team_id = tm.team_id
           WHERE tm.account_id = %s ORDER BY t.name""",
        (who.account_id,),
    ).fetchall()
    return {
        "teams": [
            {"teamId": r["team_id"], "name": r["name"],
             "createdAt": r["created_at"], "role": r["role"]}
            for r in rows
        ]
    }


@router.post("", status_code=201)
def create_team(body: CreateTeamBody, who: tokens.Principal = Depends(principal),
                conn=Depends(get_conn)):
    team_id = ids.new_id(ids.TEAM)
    now = now_ms()
    with conn.transaction():
        conn.execute(
            "INSERT INTO team (team_id, name, created_at) VALUES (%s, %s, %s)",
            (team_id, body.name, now),
        )
        # The creator is an admin, in the same transaction. A team that
        # committed without one would be a roster nobody can correct, and the
        # repos on it would stay visible forever.
        conn.execute(
            """INSERT INTO team_member (team_id, account_id, role, joined_at)
               VALUES (%s, %s, %s, %s)""",
            (team_id, who.account_id, ADMIN, now),
        )
    return {"teamId": team_id, "name": body.name, "createdAt": now, "role": ADMIN}


@router.get("/{team_id}/members")
def list_members(team_id: str, who: tokens.Principal = Depends(principal),
                 conn=Depends(get_conn)):
    _role(conn, team_id, who.account_id)
    rows = conn.execute(
        """SELECT tm.account_id, tm.role, tm.joined_at, a.actor
           FROM team_member tm JOIN account a ON a.account_id = tm.account_id
           WHERE tm.team_id = %s ORDER BY a.actor""",
        (team_id,),
    ).fetchall()
    return {
        "members": [
            {"accountId": r["account_id"], "actor": r["actor"],
             "role": r["role"], "joinedAt": r["joined_at"]}
            for r in rows
        ]
    }


@router.post("/{team_id}/members", status_code=201)
def add_member(team_id: str, body: AddMemberBody,
               who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    _require_admin(conn, team_id, who.account_id)
    if body.role not in (ADMIN, MEMBER):
        raise conflict("invalid_role", "role must be 'member' or 'admin'.")
    exists = conn.execute(
        "SELECT actor FROM account WHERE account_id = %s", (body.accountId,)
    ).fetchone()
    if exists is None:
        raise not_found("No such account.")
    conn.execute(
        """INSERT INTO team_member (team_id, account_id, role, joined_at)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (team_id, account_id) DO UPDATE SET role = excluded.role""",
        (team_id, body.accountId, body.role, now_ms()),
    )
    return {"teamId": team_id, "accountId": body.accountId,
            "actor": exists["actor"], "role": body.role}


@router.delete("/{team_id}/members/{account_id}", status_code=204)
def remove_member(team_id: str, account_id: str,
                  who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    _require_admin(conn, team_id, who.account_id)
    target = conn.execute(
        "SELECT role FROM team_member WHERE team_id = %s AND account_id = %s",
        (team_id, account_id),
    ).fetchone()
    if target is None:
        raise not_found("That account is not on this team.")
    if target["role"] == ADMIN:
        admins = conn.execute(
            "SELECT count(*) AS n FROM team_member WHERE team_id = %s AND role = %s",
            (team_id, ADMIN),
        ).fetchone()["n"]
        if admins <= 1:
            # A team with no admin is a roster nobody can correct, and the
            # repos on it stay visible to everybody on it forever.
            raise conflict("last_admin", "A team must keep at least one admin.")
    conn.execute(
        "DELETE FROM team_member WHERE team_id = %s AND account_id = %s",
        (team_id, account_id),
    )
    return Response(status_code=204)


@router.get("/{team_id}/repos")
def list_repos(team_id: str, who: tokens.Principal = Depends(principal),
               conn=Depends(get_conn)):
    _role(conn, team_id, who.account_id)
    rows = conn.execute(
        """SELECT tr.repo_id, tr.branch_names_published, tr.added_at,
                  r.name, r.remote_url, r.forge, r.owner, r.repo, r.web_url
           FROM team_repo tr
           LEFT JOIN published_repo r ON r.repo_id = tr.repo_id
           WHERE tr.team_id = %s ORDER BY r.name NULLS LAST, tr.repo_id""",
        (team_id,),
    ).fetchall()
    return {
        "repos": [
            {"repoId": r["repo_id"], "name": r["name"], "remoteUrl": r["remote_url"],
             "forge": r["forge"], "owner": r["owner"], "repo": r["repo"],
             "webUrl": r["web_url"], "addedAt": r["added_at"],
             "branchNamesPublished": r["branch_names_published"]}
            for r in rows
        ]
    }


@router.post("/{team_id}/repos", status_code=201)
def add_repo(team_id: str, body: AddRepoBody,
             who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    _require_admin(conn, team_id, who.account_id)

    # The rule from this module's docstring, first half. An admin may widen
    # who sees a repo; they may never widen which repos they themselves can
    # see, or a guessed `repo_id` becomes a way to read somebody's work on a
    # repo the guesser has no access to at all.
    #
    # The second half is not here and cannot be: what this grant is WORTH is
    # decided in `scope.resolve` from `added_by`, because only there is the
    # adder's access re-checked at read time against how things stand now.
    caller_scope = scope_mod.resolve(conn, who.account_id)
    if body.repoId not in caller_scope:
        raise not_found(
            "No such repo, or it is not one you can see. A repo can only be "
            "added to a roster by somebody who already has access to it."
        )

    # `added_by` is refreshed on a re-add, and that is the documented way to
    # UPGRADE a roster entry: an admin with verified repo access adding a
    # repo somebody else rostered from publisher-level access turns the
    # team's partial view into a full one. It can downgrade too, which is
    # the safe direction and is undone the same way.
    conn.execute(
        """INSERT INTO team_repo
               (team_id, repo_id, branch_names_published, added_at, added_by)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (team_id, repo_id) DO UPDATE
               SET branch_names_published = excluded.branch_names_published,
                   added_by = excluded.added_by""",
        (team_id, body.repoId, body.branchNamesPublished, now_ms(), who.account_id),
    )
    return {"teamId": team_id, "repoId": body.repoId,
            "branchNamesPublished": body.branchNamesPublished}


@router.patch("/{team_id}/repos/{repo_id}")
def set_branch_switch(team_id: str, repo_id: str, body: BranchSwitchBody,
                      who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    """docs/ACCOUNTS.md §5 rule 2: branch names get a per-repo opt-out.

    Default on -- they are publishable under the repo rule, since anyone with
    repo access can run `git branch -r`. Off because they are free text, and
    `feat/restricted-org-dbs` can say more than its author meant.

    This takes effect at READ time, which is the only way it can work: its
    whole purpose is to be flipped AFTER the rows were published. What makes
    that safe is the schema, not this handler -- the name lives in its own
    table, so a query that does not join it cannot return one.
    """
    _require_admin(conn, team_id, who.account_id)
    row = conn.execute(
        """UPDATE team_repo SET branch_names_published = %s
           WHERE team_id = %s AND repo_id = %s RETURNING repo_id""",
        (body.branchNamesPublished, team_id, repo_id),
    ).fetchone()
    if row is None:
        raise not_found("That repo is not on this team's roster.")
    return {"teamId": team_id, "repoId": repo_id,
            "branchNamesPublished": body.branchNamesPublished}


@router.delete("/{team_id}/repos/{repo_id}", status_code=204)
def remove_repo(team_id: str, repo_id: str,
                who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    _require_admin(conn, team_id, who.account_id)
    conn.execute(
        "DELETE FROM team_repo WHERE team_id = %s AND repo_id = %s", (team_id, repo_id)
    )
    # Idempotent, and no 404 for a repo that was not on the roster: the
    # caller's goal -- "this team cannot see that repo" -- is true either way,
    # and a 404 here would report on a roster they are entitled to read
    # anyway. Removing access is never made harder than granting it.
    return Response(status_code=204)
