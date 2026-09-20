"""Who may see which repo, and whose rows in it. The one place that decides.

docs/REDACTION.md §1 states the boundary in one sentence:

    A row may be published only if it belongs to a repo, and only to people
    who can already see that repo.

This module turns that into a set of `repo_id`s, and **every team endpoint
starts by calling `resolve`**. Not "should":
`test_every_team_read_endpoint_is_scoped` enumerates the team router's GET
routes rather than naming them, calls each one as somebody outside the repo,
and fails if any of them says a word about it -- so an endpoint added next
month is covered the day it is written, which is the only kind of coverage
that survives a busy week.

One implementation, deliberately, because two would eventually disagree about
the one thing that must not be wrong -- the same argument `cci install` used
for "which job is loaded".

WHY A SELF-ASSERTED REPO ID IS NOT EVIDENCE OF ANYTHING.
This module used to treat a row in `repo_publisher` as independently
sufficient evidence of repo access, so that publishing to a repo put you in
scope for it. That was a hole straight through the boundary, and the reason is
the property `repo_id` is *designed* to have:

    repo_id = sha256("repo" + normalized_remote)

docs/REDACTION.md §0 establishes that publishing the id is safe BECAUSE the
remote is already public on the far side -- there is nothing to confirm. The
flip side is that the id is computable by anyone who can guess the remote, and
`repoid.py` is four lines that are in the published contract. So two HTTP
calls -- publish `sha256` of a guessed remote, then read -- returned a
stranger's sessions, actors, branch names and totals.

The fix is not a better hash and cannot be. It is that **publishing is not a
grant**. What you publish, you may read back: that is a grant on the ROWS
(`account_id = you`), not on the repo. Seeing OTHER people's rows needs
something that was checked rather than asserted, which leaves two things --
a forge identity verified against GitHub (§4.6), or a team roster, which is
itself only as strong as the access of the admin who added the repo. Hence
`ROSTER_DELEGATES` below.

WHY THIS IS APPLICATION LOGIC AND NOT ROW-LEVEL SECURITY.
docs/ACCOUNTS.md §3 weighed both. An RLS policy missing from one table is a
silent total leak: the queries keep working, nothing surfaces it. And two of
the three rules in §5 -- scope-local aggregation and the per-repo branch
opt-out -- are not expressible as a row predicate at all. The second half of
that is visible right here: `Scope` carries `branch_suppressed`, which is a
rule about a COLUMN of a row the viewer is otherwise allowed to read.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The three reasons a repo can be in scope. Reported on `GET /v1/team/repos`
#: so a person can answer "why can I see this" without asking an admin.
VIA_TEAM = "team"
VIA_PUBLISHED = "published"
VIA_FORGE = "github"

#: Sole source of FULL access that does not come from another account: a
#: forge identity checked against GitHub during the one request where the
#: token existed (docs/SERVER_API.md §4.6). Everything else in this module is
#: either the caller's own rows or a delegation of somebody's verified access.
#:
#: A roster delegates only the adder's VERIFIED access, not their delegated
#: access. That refusal to chain is deliberate: following the chain would mean
#: a team's reach depended on a graph of rosters nobody can see the whole of,
#: and the failure mode of getting the traversal wrong is disclosure. Capping
#: it under-shares, which is the direction to be wrong in, and the fix is one
#: admin with repo access re-adding the repo.
ROSTER_DELEGATES = "verified access only"


@dataclass(frozen=True)
class Scope:
    """One caller's visible world, resolved once per request.

    Two sets, because "which repos can I see" and "whose rows can I see in
    them" stopped being the same question the moment publishing stopped being
    a grant:

    - `repo_ids` -- repos where the caller may read **everyone's** rows. This
      is the set that means "access to the repository" in the sense
      docs/REDACTION.md §1 uses, and it is the set `teams.add_repo` delegates
      from. It comes only from things that were checked: a verified forge
      identity, or a roster whose adder had one.

    - `partial_repo_ids` -- repos where the caller may read **some** rows and
      not others: their own, because they published them, and an admin's,
      because that admin put the repo on a roster the caller is on. You can
      always see what you sent -- that is what keeps a first publisher able
      to check their own data before anybody else can look at any of it --
      and you can see what somebody shared, but neither of those is access
      to the repository.

    `rows` carries the (repo_id, account_id) pairs that are readable
    individually -- the caller's own rows, plus the rows of an admin who put
    the repo on a roster the caller is on. Every read query ANDs
    "in `repo_ids`, OR one of these pairs" onto the scope predicate.

    A union rather than an intersection, because the rule is "people who can
    already see that repo" and each term is independently sufficient. What is
    no longer a term is the caller's own say-so.
    """

    account_id: str
    #: Repos where every row is readable. Verified access, or delegated from it.
    repo_ids: frozenset[str]
    #: (repo_id, account_id) pairs readable one row-owner at a time.
    rows: frozenset[tuple[str, str]]
    team_ids: frozenset[str]
    #: Repos where a team has switched branch names off. docs/ACCOUNTS.md §5
    #: rule 2. Populated for every path to the repo, not just the team one.
    branch_suppressed: frozenset[str]
    via: dict[str, tuple[str, ...]]

    @property
    def partial_repo_ids(self) -> frozenset[str]:
        """Repos the caller can see into but not across. Derived, never stored.

        Derived from `rows` rather than kept beside it so the two cannot
        disagree -- a repo listed here with no readable pair would be a repo
        disclosed on nothing at all, which is the shape of the bug this
        module exists to have fixed.
        """
        return frozenset(r for r, _ in self.rows) - self.repo_ids

    @property
    def visible_repo_ids(self) -> frozenset[str]:
        """Every repo the caller can see anything at all in.

        The set a `?repo=` filter is narrowed against, and the set
        `GET /v1/team/repos` lists. NOT the set that grants sight of other
        people's rows -- that is `repo_ids`, and confusing the two is the
        whole of the bug this module's docstring describes.
        """
        return self.repo_ids | self.partial_repo_ids

    def __contains__(self, repo_id: str) -> bool:
        """"I can see something here." Deliberately the WIDE set.

        `teams.add_repo` asks this question, and the answer has to be the
        wide one or a team could never be given a repo on an instance with no
        GitHub app configured -- which docs/SERVER_API.md §4.6 requires to
        keep working ("runs on rosters alone"). What makes that safe is not
        this check but `ROSTER_DELEGATES`: a roster hands the team the
        adder's own access, so an admin who can see only their own rows
        shares only their own rows. A roster still cannot widen what the
        person doing the widening can see.
        """
        return repo_id in self.visible_repo_ids

    def narrow(self, requested: list[str] | None) -> list[str]:
        """The repos to query, given an optional `?repo=` filter.

        An id outside the scope is DROPPED, not rejected. A 400 or a 404 for
        an unknown repo would answer "does this repo exist" for a caller with
        no right to ask -- the same leak as an aggregate that spans outside the
        scope, arriving through the error path instead of the data path.

        The returned list is always the authority. A caller that asks for
        nothing gets their whole scope; a caller that asks for something
        outside it gets an empty list and therefore empty results, which is
        indistinguishable from a repo with no activity.
        """
        if not requested:
            return sorted(self.visible_repo_ids)
        return sorted(set(requested) & self.visible_repo_ids)

    def row_pairs(self) -> tuple[list[str], list[str]]:
        """`rows` as two parallel arrays, for `unnest` in the read predicate.

        Two arrays rather than a composite type because psycopg adapts a list
        of strings and nothing else is needed: the predicate is
        `(sp.repo_id, sp.account_id) IN (SELECT * FROM unnest(%s, %s))`, which
        is empty-safe when the caller has no individually readable rows.
        """
        pairs = sorted(self.rows)
        return [r for r, _ in pairs], [a for _, a in pairs]

    def shows_branch(self, repo_id: str) -> bool:
        return repo_id not in self.branch_suppressed


def resolve(conn, account_id: str) -> Scope:
    """Compute the caller's scope. Called by every team endpoint, first.

    Several small queries rather than one join, because the `via` breakdown is
    part of the answer and because each term is independently testable. On the
    numbers this project actually has -- tens of repos, one team -- the cost of
    the extra round trips is not measurable, and a single clever query whose
    outer join accidentally dropped a term would be a silent over- or
    under-disclosure.
    """
    via: dict[str, set[str]] = {}
    full: set[str] = set()
    rows: set[tuple[str, str]] = set()

    team_ids = {
        r["team_id"]
        for r in conn.execute(
            "SELECT team_id FROM team_member WHERE account_id = %s", (account_id,)
        )
    }

    # TERM 3 first, because the other two are defined against it: forge access
    # is the only evidence in this system that somebody can actually reach a
    # repository, and it was checked against GitHub rather than asserted here.
    for r in conn.execute(
        "SELECT repo_id FROM account_repo_access WHERE account_id = %s", (account_id,)
    ):
        via.setdefault(r["repo_id"], set()).add(VIA_FORGE)
        full.add(r["repo_id"])

    # TERM 1, delegated. `added_by` is the admin who put the repo on the
    # roster, and the team gets what that admin had: everything, if they were
    # forge-verified for the repo; otherwise their own rows and nobody else's.
    #
    # That is what stops the roster reopening the hole this module's docstring
    # describes one step later. Without it, a guessed `repo_id` is still two
    # calls from a stranger's sessions: publish a row, roster the repo to a
    # team of one, read everything. `teams.add_repo`'s scope check cannot
    # close that on its own, because a legitimate first publisher looks
    # exactly like the attacker at the moment they roster their own repo.
    roster = conn.execute(
        """SELECT tr.repo_id, tr.team_id, tr.branch_names_published, tr.added_by,
                  EXISTS (SELECT 1 FROM account_repo_access ara
                          WHERE ara.account_id = tr.added_by
                            AND ara.repo_id = tr.repo_id) AS adder_verified
           FROM team_repo tr
           JOIN team_member tm ON tm.team_id = tr.team_id
           WHERE tm.account_id = %s""",
        (account_id,),
    ).fetchall()

    for r in roster:
        via.setdefault(r["repo_id"], set()).add(f"{VIA_TEAM}:{r['team_id']}")
        if r["adder_verified"]:
            full.add(r["repo_id"])
        elif r["added_by"] is not None:
            rows.add((r["repo_id"], r["added_by"]))

    # TERM 2, and it is no longer a repo-level grant. Read off the rows the
    # account actually published, NOT off `repo_publisher`: a `repo_publisher`
    # row is one POST of a guessed id, whereas a `published_session` row is
    # the caller's own data, and reading your own data back discloses nothing.
    #
    # The difference is visible on `GET /v1/team/repos`, which must not list a
    # repo merely because somebody claimed it -- a listing that appeared on
    # assertion alone would confirm a guessed remote.
    for r in conn.execute(
        "SELECT DISTINCT repo_id FROM published_session WHERE account_id = %s",
        (account_id,),
    ):
        via.setdefault(r["repo_id"], set()).add(VIA_PUBLISHED)
        rows.add((r["repo_id"], account_id))

    # A repo nobody has published to is not visible even if a roster names it:
    # there is nothing to see, and listing it would disclose that an admin
    # added it. The join is against `published_repo` rather than against the
    # roster for exactly that reason.
    known = {
        r["repo_id"]
        for r in conn.execute(
            "SELECT repo_id FROM published_repo WHERE repo_id = ANY(%s::text[])",
            (list(via),),
        )
    }

    return Scope(
        account_id=account_id,
        repo_ids=frozenset(full & known),
        rows=frozenset((r, a) for (r, a) in rows if r in known),
        team_ids=frozenset(team_ids),
        branch_suppressed=_branch_suppressed(conn, team_ids, known & set(via)),
        via={k: tuple(sorted(v)) for k, v in via.items() if k in known},
    )


def _branch_suppressed(conn, team_ids: set[str], repo_ids: set[str]) -> frozenset[str]:
    """Repos where the caller must not be shown a branch name. OFF WINS.

    docs/ACCOUNTS.md §5 rule 2 says "default on, one switch per repo", and the
    switch is stored per (team, repo) so that an admin of one team cannot
    change what another team is shown. Both of those are still true. What was
    missing is that the switch only ever consulted rosters reached through
    `team_member`, so **the two other paths to a repo ignored it entirely**:
    an org contractor with GitHub read access and no cci team saw the branch
    name an admin had turned off, and `GET /v1/team/repos` told them
    `branchNamesPublished: true` while doing it.

    So the rule has two halves, and the second is the fix:

      1. If any team the caller is ON has the repo rostered with the switch
         off, it is off. That is the per-team control, unchanged, and off wins
         over another of the caller's teams having it on.

      2. If the caller reaches the repo by NO team of theirs -- forge access,
         or their own publication -- then no team of theirs can speak for
         them, and the strictest switch anybody set applies. Any team with it
         off turns it off.

    The second half is not the first half in disguise. A caller inside a team
    is governed by their own teams, which is the authority boundary the schema
    comment in 003 draws; a caller outside every team has no such boundary to
    be governed by, and the choice is then between "the admin's opt-out means
    nothing to this person" and "it means what it says". The switch exists to
    stop a name being shown.
    """
    if not repo_ids:
        return frozenset()
    rows = conn.execute(
        """SELECT repo_id, team_id, branch_names_published
           FROM team_repo WHERE repo_id = ANY(%s::text[])""",
        (sorted(repo_ids),),
    ).fetchall()

    suppressed: set[str] = set()
    for repo_id in repo_ids:
        entries = [r for r in rows if r["repo_id"] == repo_id]
        mine = [r for r in entries if r["team_id"] in team_ids]
        governing = mine or entries
        if any(not r["branch_names_published"] for r in governing):
            suppressed.add(repo_id)
    return frozenset(suppressed)


def verified(conn, account_id: str, repo_ids: list[str]) -> set[str]:
    """The subset of `repo_ids` this account was VERIFIED to reach.

    Deliberately not `resolve`. It answers a narrower question -- "did GitHub
    say yes to this person for this repo" -- and it answers it for repos that
    may not be in `published_repo` yet, which `resolve` intersects away.
    `_publish_repos` needs exactly that: the caller may refresh the shared
    registry row for a repo they demonstrably have access to, including the
    first time anybody publishes it.
    """
    if not repo_ids:
        return set()
    return {
        r["repo_id"]
        for r in conn.execute(
            """SELECT repo_id FROM account_repo_access
               WHERE account_id = %s AND repo_id = ANY(%s::text[])""",
            (account_id, sorted(repo_ids)),
        )
    }
