"""Who may see which repo. The one place that decides.

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


@dataclass(frozen=True)
class Scope:
    """One caller's visible world, resolved once per request.

    `repo_ids` is the union of three things (docs/SERVER_API.md §4):

      1. repos on the roster of any team the caller belongs to;
      2. repos the caller has published to themselves -- you can always see
         what you sent;
      3. repos the caller's forge identity was verified to reach at their last
         sign-in (§4.6).

    A union, not an intersection, because the rule is "people who can already
    see that repo" and each of the three is independently sufficient evidence
    of that. Requiring a team roster on top would mean two people with
    identical GitHub access could not see each other's work until an admin
    typed something, which is the "hand-maintained" scope docs/ACCOUNTS.md §1
    set out to avoid.
    """

    account_id: str
    repo_ids: frozenset[str]
    team_ids: frozenset[str]
    #: Repos where a team the caller belongs to has switched branch names off.
    #: docs/ACCOUNTS.md §5 rule 2.
    branch_suppressed: frozenset[str]
    via: dict[str, tuple[str, ...]]

    def __contains__(self, repo_id: str) -> bool:
        return repo_id in self.repo_ids

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
            return sorted(self.repo_ids)
        return sorted(set(requested) & self.repo_ids)

    def shows_branch(self, repo_id: str) -> bool:
        return repo_id not in self.branch_suppressed


def resolve(conn, account_id: str) -> Scope:
    """Compute the caller's scope. Called by every team endpoint, first.

    Four small queries rather than one join, because the `via` breakdown is
    part of the answer and because each term is independently testable. On the
    numbers this project actually has -- tens of repos, one team -- the cost of
    the extra round trips is not measurable, and a single clever query whose
    outer join accidentally dropped a term would be a silent over- or
    under-disclosure.
    """
    via: dict[str, set[str]] = {}

    team_ids = {
        r["team_id"]
        for r in conn.execute(
            "SELECT team_id FROM team_member WHERE account_id = %s", (account_id,)
        )
    }

    roster = conn.execute(
        """SELECT tr.repo_id, tr.team_id, tr.branch_names_published
           FROM team_repo tr
           JOIN team_member tm ON tm.team_id = tr.team_id
           WHERE tm.account_id = %s""",
        (account_id,),
    ).fetchall()

    suppressed: set[str] = set()
    for r in roster:
        via.setdefault(r["repo_id"], set()).add(f"{VIA_TEAM}:{r['team_id']}")
        # OFF WINS. The switch exists to stop a branch name being shown, and
        # "another team had it on" is not a reason to show it. An admin of one
        # team cannot re-enable what an admin of another turned off.
        if not r["branch_names_published"]:
            suppressed.add(r["repo_id"])

    for r in conn.execute(
        "SELECT repo_id FROM repo_publisher WHERE account_id = %s", (account_id,)
    ):
        via.setdefault(r["repo_id"], set()).add(VIA_PUBLISHED)

    for r in conn.execute(
        "SELECT repo_id FROM account_repo_access WHERE account_id = %s", (account_id,)
    ):
        via.setdefault(r["repo_id"], set()).add(VIA_FORGE)

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
        repo_ids=frozenset(known),
        team_ids=frozenset(team_ids),
        branch_suppressed=frozenset(suppressed & known),
        via={k: tuple(sorted(v)) for k, v in via.items() if k in known},
    )
