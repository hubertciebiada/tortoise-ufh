"""Regression guard: the HA integration is a self-contained HACS install.

HACS ``category: integration`` ships **only** ``custom_components/tortoise_ufh/``.
Before the core was vendored, the adapter imported a sibling top-level package
``tortoise_ufh`` with absolute imports (``from tortoise_ufh.config import ...``);
that package was absent after a HACS copy, so the config flow failed to register
(``Invalid handler specified``). See ``docs/HACS_INSTALL_BUG.md``.

These tests lock in the fix (Option A — vendor the core at
``custom_components/tortoise_ufh/core/``):

* no adapter file outside the ``core/`` subtree may carry a bare top-level
  ``tortoise_ufh`` import (that absolute import *is* the bug);
* the vendored core exists at the new location with its key modules; and
* the old top-level ``tortoise_ufh/`` package directory is gone.

This module only scans files on disk — it imports neither ``homeassistant`` nor
the adapter — so it runs in the pure-core (HA-free) unit environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/unit/test_packaging_selfcontained.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION = _REPO_ROOT / "custom_components" / "tortoise_ufh"
_CORE = _INTEGRATION / "core"

# A bare, top-level import of the (now non-existent) sibling core package. Matches
# ``from tortoise_ufh ...`` / ``from tortoise_ufh.x ...`` / ``import tortoise_ufh``
# (optionally indented) but NOT ``from custom_components.tortoise_ufh...`` and NOT
# an identifier that merely starts with ``tortoise_ufh`` (e.g. ``tortoise_ufh_panel``).
_BARE_CORE_IMPORT = re.compile(r"^\s*(from|import)\s+tortoise_ufh(\.|\s|$)")


def _adapter_files_excluding_core() -> list[Path]:
    """Every ``*.py`` under the integration except the vendored ``core/`` subtree.

    Returns:
        Sorted list of Python source paths in the HA adapter layer.
    """
    return sorted(
        path
        for path in _INTEGRATION.rglob("*.py")
        if _CORE not in path.parents and path != _CORE
    )


@pytest.mark.unit
def test_adapter_has_no_bare_core_imports() -> None:
    """No adapter file (outside ``core/``) imports the bare ``tortoise_ufh`` package."""
    adapter_files = _adapter_files_excluding_core()
    assert adapter_files, "expected to find adapter Python files to scan"

    offenders: list[str] = []
    for path in adapter_files:
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _BARE_CORE_IMPORT.match(line):
                rel = path.relative_to(_REPO_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "adapter files must import the core via a relative `.core` import, not the "
        "bare top-level `tortoise_ufh` package (that absolute import breaks a HACS "
        "install — see docs/HACS_INSTALL_BUG.md). Offending lines:\n"
        + "\n".join(offenders)
    )


@pytest.mark.unit
def test_vendored_core_exists_with_key_modules() -> None:
    """The pure core is vendored at ``custom_components/tortoise_ufh/core/``."""
    assert _CORE.is_dir(), f"vendored core package missing at {_CORE}"

    for name in ("models.py", "config.py", "controller.py", "__init__.py", "py.typed"):
        assert (_CORE / name).is_file(), f"vendored core is missing {name}"


@pytest.mark.unit
def test_no_top_level_core_package() -> None:
    """The old top-level ``tortoise_ufh/`` package directory no longer exists."""
    stale = _REPO_ROOT / "tortoise_ufh"
    assert not stale.exists(), (
        f"the top-level core package {stale} must not exist after vendoring; "
        "the core now lives at custom_components/tortoise_ufh/core/"
    )
