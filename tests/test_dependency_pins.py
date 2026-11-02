"""The dependency files must agree with each other.

Two packages are pinned in more than one file — sqlmodel and cryptography appear
in both requirements-dev.txt and backend/requirements-lock.txt — and nothing
checked that they match. Dependabot bumps one file at a time, so the moment one
is updated the other silently falls behind and CI keeps passing while the two
environments diverge.

These tests fail when that happens, and also when a lockfile stops covering a
package that requirements.txt asks for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Every requirements file in the repo, with whether it is a lockfile.
REQ_FILES = {
    "requirements.txt": False,
    "requirements-lock.txt": True,
    "requirements-dev.txt": False,
    "requirements-livekit.txt": False,
    "backend/requirements.txt": False,
    "backend/requirements-lock.txt": True,
}

# Lines look like: name==1.2.3, name[extra]>=1.0, name>=1.0 ; sys_platform == 'win32'
_REQ = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(\[[^\]]*\])?\s*(==|>=|~=|>|<)?\s*([^\s;#]+)?")


def _canonical(name: str) -> str:
    """PEP 503-ish normalisation, so sql-model and sql_model match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(path: Path) -> "dict[str, tuple[str, str]]":
    """{package: (operator, version)} for every requirement line in a file."""
    pins: "dict[str, tuple[str, str]]" = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ.match(line)
        if not m:
            continue
        name, _extras, op, version = m.groups()
        pins[_canonical(name)] = (op or "", version or "")
    return pins


def _existing() -> "dict[str, Path]":
    return {rel: ROOT / rel for rel in REQ_FILES if (ROOT / rel).is_file()}


@pytest.mark.parametrize("rel", sorted(REQ_FILES))
def test_requirements_file_exists(rel):
    """A file named in this test that disappears is a file someone stopped maintaining."""
    assert (ROOT / rel).is_file(), f"{rel} is missing"


def test_pins_are_consistent_across_files():
    """A package pinned in two places must be pinned to the same version."""
    pinned: "dict[str, dict[str, str]]" = {}
    for rel, path in _existing().items():
        for name, (op, version) in _parse(path).items():
            if op == "==" and version:
                pinned.setdefault(name, {})[rel] = version

    conflicts = {
        name: versions
        for name, versions in pinned.items()
        if len(set(versions.values())) > 1
    }
    assert not conflicts, f"the same package is pinned to different versions: {conflicts}"


def test_lockfiles_pin_everything_they_claim_to():
    """A lockfile that leaves a package unpinned is not a lockfile."""
    for rel, path in _existing().items():
        if not REQ_FILES[rel]:
            continue
        loose = [n for n, (op, _v) in _parse(path).items() if op != "=="]
        # The windows-only pyttsx3 line is deliberately left unpinned (it is not
        # installed off Windows); everything else in a lock must be exact.
        loose = [n for n in loose if n != "pyttsx3"]
        assert not loose, f"{rel} leaves these unpinned: {loose}"


def test_root_lock_covers_every_root_requirement():
    """Everything requirements.txt asks for must be in requirements-lock.txt.

    The lock is what CI and `make install` use, so a package added to
    requirements.txt and never re-frozen would be missing from every install.
    """
    asked = _parse(ROOT / "requirements.txt")
    locked = _parse(ROOT / "requirements-lock.txt")
    missing = sorted(set(asked) - set(locked))
    assert not missing, (
        f"requirements.txt asks for {missing} but requirements-lock.txt does not "
        f"pin them — run `make lock`"
    )


def test_backend_lock_covers_every_backend_requirement():
    asked = _parse(ROOT / "backend" / "requirements.txt")
    locked = _parse(ROOT / "backend" / "requirements-lock.txt")
    missing = sorted(set(asked) - set(locked))
    assert not missing, (
        f"backend/requirements.txt asks for {missing} but the lock does not pin "
        f"them — re-resolve backend/requirements-lock.txt"
    )


def test_dev_set_carries_the_backend_runtime_packages():
    """CI runs backend/tests/, so the dev environment must import the backend app.

    This is the regression that made `import backend.app.db` fail in CI: the root
    lockfile has no sqlmodel or cryptography because they belong to backend/.
    """
    dev = _parse(ROOT / "requirements-dev.txt")
    for package in ("sqlmodel", "cryptography"):
        assert package in dev, (
            f"{package} must be pinned in requirements-dev.txt or the backend app "
            f"cannot be imported in CI"
        )


def test_dev_set_includes_the_root_lockfile():
    """The dev set must stay a superset of the reproducible set, not a parallel one."""
    text = (ROOT / "requirements-dev.txt").read_text()
    assert re.search(r"^-r\s+requirements-lock\.txt\s*$", text, re.M), (
        "requirements-dev.txt must pull in requirements-lock.txt so the version "
        "set CI tests is the one the lockfile pins"
    )
