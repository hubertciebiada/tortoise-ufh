"""i18n parity guards for the panel and the HA translation JSONs.

The sidebar panel keeps every user-facing string in ONE table per language
(``const STR = { pl: {...}, en: {...} }`` in ``tortoise-ufh-panel.js``) with an
English fallback; adding a language = adding one dictionary. These tests keep
that architecture honest without a JS runtime:

* ``STR.pl`` and ``STR.en`` carry the exact same key set;
* every ``FLAG_LABELS`` entry carries ``pl``, ``en`` and ``sev``;
* every controller knob exposed in ``const.CONTROLLER_NUMBER_KNOBS`` (plus the
  boolean knob) has both a ``tune_<name>`` label and a ``tip_knob_<name>``
  tooltip in BOTH languages;
* ``strings.json``, ``translations/en.json`` and ``translations/pl.json`` carry
  identical key sets (the triple must always change together).

Everything is parsed TEXTUALLY from the files on disk — this module imports
neither ``homeassistant`` nor the adapter, so it runs in the pure-core
(HA-free) unit environment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

# tests/unit/test_panel_i18n.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION = _REPO_ROOT / "custom_components" / "tortoise_ufh"
_PANEL_JS = _INTEGRATION / "frontend" / "tortoise-ufh-panel.js"
_CONST_PY = _INTEGRATION / "const.py"
_STRINGS = _INTEGRATION / "strings.json"
_TRANSLATIONS = _INTEGRATION / "translations"

# A dictionary key line inside a language section of STR: 4-space indent,
# identifier, colon. Multi-line string continuations (6-space indent, quote
# first) and comments (``//``) do not match.
_STR_KEY = re.compile(r"^ {4}([A-Za-z0-9_]+):", re.MULTILINE)

# One FLAG_LABELS entry: ``code: { ...body without braces... }``.
_FLAG_ENTRY = re.compile(r"(\w+):\s*\{([^{}]*)\}", re.DOTALL)

# A numeric knob tuple in const.CONTROLLER_NUMBER_KNOBS: ``("name", ...``.
_KNOB_TUPLE = re.compile(r"\(\s*\"(\w+)\",")

_BOOL_KNOB = re.compile(r"CONTROLLER_BOOL_KNOB:\s*str\s*=\s*\"(\w+)\"")


def _extract_block(source: str, marker: str) -> str:
    """Return the ``{...}`` block that starts at ``marker`` (ends at ``\\n};``).

    Args:
        source: The full JS module text.
        marker: The declaration prefix, e.g. ``"const STR = {"``.

    Returns:
        The block text between the marker and the first column-0 ``};``.

    Raises:
        AssertionError: If the marker or terminator is missing.
    """
    start = source.find(marker)
    assert start >= 0, f"marker {marker!r} not found in the panel module"
    end = source.find("\n};", start)
    assert end >= 0, f"unterminated block for {marker!r}"
    return source[start + len(marker) : end]


def _str_language_keys(source: str) -> dict[str, set[str]]:
    """Parse the STR table into ``{language: {keys}}``.

    Args:
        source: The full JS module text.

    Returns:
        A mapping of language code to its key set.
    """
    block = _extract_block(source, "const STR = {")
    sections: dict[str, set[str]] = {}
    # Language sections sit at 2-space indent: ``  pl: {`` ... ``  },``.
    for match in re.finditer(r"^ {2}(\w+): \{$", block, re.MULTILINE):
        lang = match.group(1)
        section_start = match.end()
        closer = block.find("\n  },", section_start)
        assert closer >= 0, f"unterminated STR.{lang} section"
        sections[lang] = set(_STR_KEY.findall(block[section_start:closer]))
    return sections


def _knob_names() -> list[str]:
    """Parse the exposed knob names textually from ``const.py``.

    Returns:
        Numeric knob names in declaration order plus the boolean knob.
    """
    text = _CONST_PY.read_text(encoding="utf-8")
    start = text.find("CONTROLLER_NUMBER_KNOBS")
    # The tuple block ends at its docstring ("""Numeric ...).
    end = text.find('"""Numeric', start)
    assert start >= 0 and end > start, "CONTROLLER_NUMBER_KNOBS block not found"
    names = _KNOB_TUPLE.findall(text[start:end])
    assert names, "no numeric knobs parsed from const.py"
    bool_match = _BOOL_KNOB.search(text)
    assert bool_match is not None, "CONTROLLER_BOOL_KNOB not found in const.py"
    return [*names, bool_match.group(1)]


def _flatten_keys(node: Any, prefix: str = "") -> set[str]:
    """Flatten a nested JSON object into dotted key paths.

    Args:
        node: The JSON value (dict or leaf).
        prefix: Dotted path accumulated so far.

    Returns:
        The set of dotted paths of every leaf value.
    """
    if not isinstance(node, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        keys |= _flatten_keys(value, path)
    return keys


@pytest.mark.unit
def test_str_languages_have_identical_key_sets() -> None:
    """STR.pl and STR.en carry exactly the same keys (PL/EN always in pairs)."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    assert set(sections) >= {"pl", "en"}, f"missing STR languages: {sections.keys()}"
    only_pl = sorted(sections["pl"] - sections["en"])
    only_en = sorted(sections["en"] - sections["pl"])
    assert not only_pl and not only_en, (
        f"STR key mismatch — only in pl: {only_pl}; only in en: {only_en}"
    )
    # Sanity: the parser actually saw a meaningful table.
    assert len(sections["pl"]) > 50


@pytest.mark.unit
def test_str_has_no_retired_shadow_key() -> None:
    """The retired shadow state left no dead key behind (v0.7.0)."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    for lang, keys in sections.items():
        assert "state_shadow" not in keys, f"dead key state_shadow in STR.{lang}"


@pytest.mark.unit
def test_flag_labels_have_pl_en_and_severity() -> None:
    """Every FLAG_LABELS entry carries pl + en + sev."""
    source = _PANEL_JS.read_text(encoding="utf-8")
    block = _extract_block(source, "const FLAG_LABELS = {")
    entries = _FLAG_ENTRY.findall(block)
    assert entries, "no FLAG_LABELS entries parsed"
    for code, body in entries:
        for field in ("pl:", "en:", "sev:"):
            assert field in body, f"FLAG_LABELS.{code} is missing {field[:-1]!r}"


@pytest.mark.unit
def test_every_knob_has_label_and_tooltip_in_both_languages() -> None:
    """Each exposed knob has tune_<name> and tip_knob_<name> in pl AND en."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    knobs = _knob_names()
    assert len(knobs) == 14, f"expected 14 exposed knobs, parsed {knobs}"
    missing: list[str] = []
    for knob in knobs:
        for key in (f"tune_{knob}", f"tip_knob_{knob}"):
            for lang in ("pl", "en"):
                if key not in sections[lang]:
                    missing.append(f"{lang}:{key}")
    assert not missing, f"missing STR keys: {missing}"


@pytest.mark.unit
def test_translation_jsons_have_identical_key_sets() -> None:
    """strings.json ≡ translations/en.json ≡ translations/pl.json (key sets)."""
    strings = _flatten_keys(json.loads(_STRINGS.read_text(encoding="utf-8")))
    en = _flatten_keys(
        json.loads((_TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    )
    pl = _flatten_keys(
        json.loads((_TRANSLATIONS / "pl.json").read_text(encoding="utf-8"))
    )
    assert strings == en, (
        f"strings.json vs en.json: only-strings={sorted(strings - en)}, "
        f"only-en={sorted(en - strings)}"
    )
    assert strings == pl, (
        f"strings.json vs pl.json: only-strings={sorted(strings - pl)}, "
        f"only-pl={sorted(pl - strings)}"
    )
