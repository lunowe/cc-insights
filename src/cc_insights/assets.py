"""Files that ship *with* the code: the built dashboard, the scheduler jobs.

Both of these used to be found by walking up from `__file__` to the source
checkout -- `parents[2] / "frontend" / "dist"`. That works for `pip install -e
.` and for nothing else. In a wheel there is no `parents[2]`: the package sits
in `site-packages`, two levels up is `lib/python3.x`, and the dashboard the
user installed simply is not there. `cci serve` then renders its "the frontend
has not been built yet, run `cd frontend && npm run build`" page, which is
advice you cannot act on because you do not have the repo.

So the packaged copy is the real one and the source tree is the fallback, not
the other way around. A contributor working in a checkout has no `web/`
directory and keeps getting `frontend/dist` with its live rebuilds; everyone
else gets the copy hatchling put inside the package. Neither has to know which
one they are.

`frontend/dist` is a build artifact -- gitignored, and absent until someone
runs the frontend build -- so packaging it is a release-time step. That is
what makes it worth a test: the failure is silent and only reaches you through
a user who installed the tool and found half of it missing.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent

#: Where the wheel puts the built dashboard. See `[tool.hatch.build.targets.
#: wheel.force-include]` in pyproject.toml, which copies `frontend/dist` here.
PACKAGED_WEB = _HERE / "web"

#: Where `npm run build` puts it in a source checkout. Only meaningful when
#: the package was installed from one; in a wheel this points into
#: `site-packages`' parent and never exists.
SOURCE_WEB = _HERE.parents[1] / "frontend" / "dist"

#: The launchd / Task Scheduler templates, which travel with the package for
#: the same reason: `cci install` must work without a repo checkout.
JOBS = _HERE / "jobs"

#: The numbered SQL migrations. Same story as the dashboard, and a worse
#: failure: `discover_migrations` on an empty directory returns an empty list
#: and `migrate` cheerfully applies nothing, so a pip-installed `cci init`
#: produced a database with no tables and no error. The repo keeps them at
#: `migrations/` (they are source, not a build artifact, and the README's
#: layout section names that path), so the wheel force-includes a copy.
PACKAGED_MIGRATIONS = _HERE / "migrations"
SOURCE_MIGRATIONS = _HERE.parents[1] / "migrations"


def frontend_dir() -> Path:
    """Where the dashboard is, preferring the packaged copy.

    Always a path, never None -- the caller renders a different page when it
    does not exist, and that page names the directory it looked in. Returning
    the source-tree path when neither exists keeps that message useful in the
    one situation where the advice ("build the frontend") actually applies.
    """
    if PACKAGED_WEB.is_dir():
        return PACKAGED_WEB
    return SOURCE_WEB


def migrations_dir() -> Path:
    """Where the schema migrations are, preferring the packaged copy.

    Unlike the dashboard this is never legitimately absent: without it the
    tool cannot create a database at all. `db.migrate` therefore treats an
    empty directory as an error rather than as "nothing to do".
    """
    if PACKAGED_MIGRATIONS.is_dir():
        return PACKAGED_MIGRATIONS
    return SOURCE_MIGRATIONS


def is_packaged() -> bool:
    """True when running from a wheel that carries its own dashboard.

    `cci doctor` uses this to tell "you installed the tool and the dashboard
    is missing" (a broken release) apart from "you are in a checkout and have
    not run the frontend build" (normal, and fixable by the printed command).
    """
    return PACKAGED_WEB.is_dir()


def job_template(name: str) -> str:
    """One scheduler template's text, from the package.

    Raises rather than returning a default: a missing template means the
    install would write a job file that does nothing, and a job that silently
    does nothing is exactly the failure this project exists to prevent -- logs
    are pruned on a rolling basis and uncaptured history does not come back.
    """
    path = JOBS / name
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} is missing from the installed package ({JOBS}). "
            "This is a packaging bug, not something you can fix locally; "
            "reinstall, or run the installer from a source checkout."
        )
    return path.read_text()
