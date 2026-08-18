"""The package imports, carries a version, and pulls in nothing it does not declare.

The second one is the load-bearing test. This package is developed inside a larger tree,
where far more than its own dependencies are importable, so an accidental import of
something outside the package would work locally and fail for everyone who installs it.

The assertion is deliberately environment-independent: importing ``baraza`` must add no
top-level module beyond the standard library and the dependencies ``pyproject.toml``
declares. Asserting instead that some particular module is *unimportable* would be a
property of the CI environment only, red on every developer's machine, and a check that is
permanently red for a reason nobody intends to fix gets muted — which is how a guard
switches itself off.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import baraza

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: Distribution names whose import name differs. Empty today, and kept so that adding such
#: a dependency is a one-line change here rather than a puzzling failure.
IMPORT_NAMES: dict[str, str] = {}

_PROBE = (
    "import sys; before = set(sys.modules); import baraza; "
    "print(','.join(sorted({m.split('.')[0] for m in set(sys.modules) - before} "
    "- sys.stdlib_module_names)))"
)


def _declared_imports() -> set[str]:
    """Every top-level module the manifest permits: the declared dependencies, plus the
    package itself."""
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names = set()
    for spec in manifest["project"]["dependencies"]:
        dist = spec.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        names.add(IMPORT_NAMES.get(dist, dist.replace("-", "_")))
    return names | {"baraza"}


def test_the_package_imports_and_reports_a_version() -> None:
    assert baraza.__version__
    assert baraza.__version__.startswith("0.")


def test_the_two_declared_versions_agree() -> None:
    """One fact in two places. `__version__` is what the app and the MCP surface report;
    the manifest is what PyPI records. A release where they disagree is one where the
    version a user can see is not the version they installed, and a wrong version cannot
    be withdrawn once it is on PyPI.
    """
    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["version"]
    assert baraza.__version__ == declared, (
        f"`baraza.__version__` is {baraza.__version__!r} but the manifest declares {declared!r}"
    )


def test_importing_the_package_pulls_in_nothing_undeclared() -> None:
    """Run in a fresh interpreter, so nothing pytest or a conftest already imported can
    mask the result."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    pulled = {name for name in result.stdout.strip().split(",") if name}
    undeclared = pulled - _declared_imports()
    assert not undeclared, (
        f"importing baraza pulled in {sorted(undeclared)}, which pyproject.toml does not "
        "declare. Anyone installing the package would get an ImportError."
    )


def test_no_artifact_pattern_ends_in_the_shape_that_matches_nothing() -> None:
    """`static/**/*` matched NOTHING and shipped a wheel with no interface.

    Hatchling requires a directory level for `**/`, so `dir/**/*` skips everything at the
    top of `dir` — which is where `index.html` is. `dir/**` matches the whole tree.

    Asserted on the pattern SHAPE rather than by globbing. `pathlib` treats `**` as zero or
    more directories, so a glob-based check matches `index.html` under both spellings and
    cannot tell the broken one from the working one — it passed against the very pattern
    that shipped the empty wheel, which is worse than no check.
    """
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = manifest["tool"]["hatch"]["build"]["artifacts"]
    assert patterns, "no artifacts declared — the compiled UI would not ship"

    broken = [p for p in patterns if p.endswith("/**/*")]
    assert not broken, (
        f"these patterns match nothing at the top of their directory: {broken}. "
        "Use `dir/**`, which covers the whole tree."
    )


def test_the_artifacts_are_declared_where_the_sdist_sees_them() -> None:
    """On the shared `[tool.hatch.build]` table, never on the wheel target alone.

    `python -m build` produces an sdist and then builds the wheel FROM it, so a file the
    sdist drops is missing from the wheel whatever the wheel target says. Declared on the
    wheel only, `python -m build --wheel` looked right and the real build shipped an API
    with no interface.
    """
    root = Path(__file__).resolve().parents[1]
    build_table = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]
    assert "artifacts" in build_table, "artifacts must be on the shared table so the sdist carries them too"
    assert "artifacts" not in build_table.get("targets", {}).get("wheel", {}), (
        "artifacts on the wheel target alone does not reach the sdist the wheel is built from"
    )
