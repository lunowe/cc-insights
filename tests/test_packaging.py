"""What the wheel actually contains, checked by building one.

This file exists because of a failure no other test could see. Every test in
this suite imports `cc_insights` from the source tree, where
`parents[2] / "frontend" / "dist"` and `parents[2] / "migrations"` both
resolve. In a wheel neither does: the package sits in `site-packages` and two
levels up is `lib/python3.x`. So the whole suite passed on a build that
shipped no dashboard and no schema -- `cci serve` rendered "run npm run build"
at someone who does not have the repo, and `cci init` applied zero migrations
and reported success, leaving an empty database.

Asserting on `pyproject.toml` would not have caught it either, because the
question is not "is the rule written down" but "did the build honour it".
Hatchling ignores `frontend/dist` by default -- it is gitignored, being a
build artifact -- so the rule has to be a `force-include` and the only honest
check is to build and look inside.

The build needs hatchling, which the base install deliberately does not have
(`pip install cc-insights` pulls in nothing). Absent, these skip -- so
`pip install -e '.[dev]'` is part of cutting a release, not optional.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from cc_insights import assets

REPO = Path(__file__).resolve().parents[1]

build = pytest.importorskip("build", reason="needs the [dev] extra to build a wheel")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    """One wheel, built once, for the whole module.

    `--no-isolation` uses this interpreter's hatchling instead of downloading
    a fresh one, which keeps the test offline and takes it from ~30s to ~2s.
    """
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(out), str(REPO)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel build failed:\n{proc.stdout}\n{proc.stderr}")
    built = list(out.glob("*.whl"))
    assert len(built) == 1, f"expected one wheel, got {built}"
    return zipfile.ZipFile(built[0])


def names(wheel: zipfile.ZipFile) -> list[str]:
    return wheel.namelist()


# ------------------------------------------------------------ the schema --


def test_the_wheel_carries_every_migration(wheel):
    """Missing these is the worst of the three: it fails silently.

    An absent dashboard is visible the moment you open the page. An absent
    migration set produced a database with no tables and an exit code of 0,
    and the next command to touch it blamed the database.
    """
    packaged = sorted(
        n.rsplit("/", 1)[-1] for n in names(wheel)
        if n.startswith("cc_insights/migrations/") and n.endswith(".sql")
    )
    on_disk = sorted(p.name for p in (REPO / "migrations").glob("*.sql"))

    assert on_disk, "no migrations in the repo -- this test is checking nothing"
    assert packaged == on_disk, (
        "the wheel's migrations do not match the repo's. A migration added to "
        "migrations/ must also reach the wheel; see the force-include in "
        "pyproject.toml."
    )


def test_an_installed_package_would_find_its_migrations(wheel):
    """The path `assets.migrations_dir()` prefers must be the packaged one."""
    assert assets.PACKAGED_MIGRATIONS.name == "migrations"
    assert assets.PACKAGED_MIGRATIONS.parent.name == "cc_insights"
    assert any(n.startswith("cc_insights/migrations/") for n in names(wheel))


def test_discover_refuses_to_find_no_migrations_by_default(monkeypatch):
    """An empty shipped directory is a packaging bug, not "nothing to do".

    The old behaviour returned `[]` and `migrate` applied nothing, which is
    how a wheel with no .sql files created an empty database and said
    "migrations applied: []" as though that were a result.
    """
    from cc_insights import db

    monkeypatch.setattr(db, "MIGRATIONS_DIR", Path("/nonexistent/migrations"))
    with pytest.raises(RuntimeError, match="packaging bug"):
        db.discover_migrations()


def test_an_explicitly_empty_directory_is_still_allowed(tmp_path):
    """Tests pass a directory on purpose; only the default is guarded."""
    from cc_insights import db

    assert db.discover_migrations(tmp_path) == []


# --------------------------------------------------------- the dashboard --


def test_the_wheel_carries_the_built_dashboard(wheel):
    entries = [n for n in names(wheel) if n.startswith("cc_insights/web/")]
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("frontend/dist is not built in this checkout")

    assert "cc_insights/web/index.html" in entries, (
        "the wheel has no dashboard. `cci serve` from a pip install would "
        "show the JSON API and tell the user to run `npm run build`, which "
        "they cannot do without the repo."
    )
    assert any(n.startswith("cc_insights/web/assets/") and n.endswith(".js")
               for n in entries), "index.html shipped without its assets"
    assert any(n.endswith(".css") for n in entries)


def test_the_packaged_dashboard_wins_over_the_source_tree(monkeypatch, tmp_path):
    """In a checkout that somehow has both, the shipped copy is the answer.

    A stale `frontend/dist` beside an installed package is the confusing case:
    the served page would silently be the older build.
    """
    packaged = tmp_path / "web"
    packaged.mkdir()
    (packaged / "index.html").write_text("packaged")

    monkeypatch.setattr(assets, "PACKAGED_WEB", packaged)
    monkeypatch.setattr(assets, "SOURCE_WEB", tmp_path / "frontend" / "dist")
    assert assets.frontend_dir() == packaged
    assert assets.is_packaged()


def test_a_checkout_falls_back_to_the_frontend_build(monkeypatch, tmp_path):
    source = tmp_path / "frontend" / "dist"
    source.mkdir(parents=True)

    monkeypatch.setattr(assets, "PACKAGED_WEB", tmp_path / "nope")
    monkeypatch.setattr(assets, "SOURCE_WEB", source)
    assert assets.frontend_dir() == source
    assert not assets.is_packaged()


def test_the_unbuilt_message_names_a_real_path(monkeypatch, tmp_path):
    """With neither present, the advice printed must still be actionable."""
    monkeypatch.setattr(assets, "PACKAGED_WEB", tmp_path / "nope")
    monkeypatch.setattr(assets, "SOURCE_WEB", tmp_path / "frontend" / "dist")
    assert assets.frontend_dir().name == "dist"


# ------------------------------------------------------ the job templates --


def test_the_wheel_carries_the_scheduler_templates(wheel):
    """`cci install` writes these on a machine that has no checkout."""
    entries = {n for n in names(wheel) if n.startswith("cc_insights/jobs/")}
    for expected in ("com.cc-insights.plist", "com.cc-insights.watch.plist",
                     "install-task.ps1"):
        assert f"cc_insights/jobs/{expected}" in entries, f"{expected} missing"


def test_job_template_reads_from_the_package():
    text = assets.job_template("com.cc-insights.plist")
    assert "__CCI__" in text and "__LOGDIR__" in text


def test_a_missing_template_says_it_is_a_packaging_bug():
    with pytest.raises(FileNotFoundError, match="packaging bug"):
        assets.job_template("com.cc-insights.nonexistent.plist")


# ---------------------------------------------------------------- the sdist --
#
# Everything above builds a wheel straight out of the checkout, which is the
# path a release takes and not the path a source build takes. `pip install
# --no-binary cc-insights`, a resolver with no matching wheel, and every
# distribution that builds from source on principle all go through the sdist
# -- and the sdist did not carry `frontend/dist`, while the wheel build
# force-includes it unconditionally. Hatchling aborts with "Forced include
# not found" when the source is missing, so those installs did not lose the
# dashboard, they failed outright. The README uploads the sdist anyway.


def _build(what: str, source: Path, outdir: Path) -> Path:
    proc = subprocess.run(
        [sys.executable, "-m", "build", what, "--no-isolation",
         "--outdir", str(outdir), str(source)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"{what} build of {source} failed:\n{proc.stdout}\n{proc.stderr}")
    built = sorted(outdir.iterdir())
    assert len(built) == 1, f"expected one artifact, got {built}"
    return built[0]


@pytest.fixture(scope="module")
def sdist(tmp_path_factory) -> Path:
    """The published source distribution, unpacked."""
    out = tmp_path_factory.mktemp("sdist")
    archive = _build("--sdist", REPO, out)
    with tarfile.open(archive) as tar:
        tar.extractall(out / "src", filter="data")
    unpacked = next((out / "src").iterdir())
    return unpacked


def test_the_sdist_carries_what_the_wheel_build_demands(sdist):
    """`frontend/dist` is gitignored, so it reaches the sdist only on purpose."""
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("frontend/dist is not built in this checkout")
    assert (sdist / "frontend" / "dist" / "index.html").is_file(), (
        "the sdist has no dashboard, and the wheel build force-includes one: "
        "every build from source fails with `Forced include not found`."
    )
    assert list((sdist / "migrations").glob("*.sql"))


def test_a_wheel_can_be_built_from_the_published_sdist(sdist, tmp_path):
    """What `pip install --no-binary cc-insights` actually does.

    Reproduced before the fix: FileNotFoundError out of hatchling, so the
    package could not be installed from source at all -- not by pip with a
    wheel unavailable, not by a distro, not by anyone auditing what they run.
    """
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("frontend/dist is not built in this checkout")

    built = _build("--wheel", sdist, tmp_path / "wheel")
    with zipfile.ZipFile(built) as whl:
        entries = whl.namelist()

    assert "cc_insights/web/index.html" in entries, "a source build lost the dashboard"
    assert any(n.startswith("cc_insights/migrations/") and n.endswith(".sql")
               for n in entries), "a source build lost the schema"


# ------------------------------------------------------------- the basics --


def test_the_wheel_exposes_the_cci_entry_point(wheel):
    entry = wheel.read("cc_insights-0.1.0.dist-info/entry_points.txt").decode()
    assert "cci = cc_insights.cli:main" in entry


def test_the_base_install_has_no_required_dependencies():
    """The dependency-free base install is a stated feature; keep it honest.

    psycopg is an extra because sync is the one thing you can go years
    without touching. A dependency that creeps into the base list makes
    `pipx install cc-insights` a different proposition.
    """
    text = (REPO / "pyproject.toml").read_text()
    assert "dependencies = []" in text
