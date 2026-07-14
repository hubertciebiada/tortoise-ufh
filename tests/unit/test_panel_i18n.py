"""i18n parity guards for the panel and the HA translation JSONs.

The sidebar panel keeps every user-facing string in ONE table per language
(``const STR = { pl: {...}, en: {...} }`` in ``tortoise-ufh-panel.js``) with an
English fallback; adding a language = adding one dictionary. These tests keep
that architecture honest without a JS runtime:

* ``STR.pl``, ``STR.en`` and ``STR.de`` carry the exact same key set;
* every ``FLAG_LABELS`` entry is a complete registry row — ``pl``/``en``/``de``
  label, ``sev``, ``sx`` (``"S#"`` or ``null``), ``group`` and
  ``descPl``/``descEn``/``descDe`` — since the flag annunciator renders entirely
  from that one map;
* every controller knob exposed in ``const.CONTROLLER_NUMBER_KNOBS`` (plus the
  boolean knob) has both a ``tune_<name>`` label and a ``tip_knob_<name>``
  tooltip in ALL languages;
* ``strings.json``, ``translations/en.json``, ``translations/pl.json`` and
  ``translations/de.json`` carry identical key sets (they must always change
  together).

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
    """STR.pl, STR.en and STR.de carry exactly the same keys (langs in lockstep)."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    assert set(sections) >= {"pl", "en", "de"}, (
        f"missing STR languages: {sections.keys()}"
    )
    only_pl = sorted(sections["pl"] - sections["en"])
    only_en = sorted(sections["en"] - sections["pl"])
    assert not only_pl and not only_en, (
        f"STR key mismatch — only in pl: {only_pl}; only in en: {only_en}"
    )
    only_de = sorted(sections["de"] - sections["en"])
    en_not_de = sorted(sections["en"] - sections["de"])
    assert not only_de and not en_not_de, (
        f"STR key mismatch — only in de: {only_de}; only in en (not de): {en_not_de}"
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


# The flag annunciator (Rooms tab) renders ENTIRELY from FLAG_LABELS, so each
# entry must be a full registry row: adding a flag = one complete entry, and it
# appears everywhere. These are the vocabularies the panel/annunciator knows.
_VALID_FLAG_SEV = {"ok", "info", "warn", "problem", "alarm"}
_VALID_FLAG_GROUP = {"safety", "assist", "config"}


@pytest.mark.unit
def test_flag_labels_are_a_complete_registry() -> None:
    """Every FLAG_LABELS entry is a full row: label+sev+sx+group+desc, valid."""
    source = _PANEL_JS.read_text(encoding="utf-8")
    block = _extract_block(source, "const FLAG_LABELS = {")
    entries = _FLAG_ENTRY.findall(block)
    assert len(entries) >= 20, f"expected >=20 flags, parsed {len(entries)}"
    fields = (
        "pl:",
        "en:",
        "de:",
        "sev:",
        "sx:",
        "group:",
        "descPl:",
        "descEn:",
        "descDe:",
    )
    for code, body in entries:
        for field in fields:
            assert field in body, f"FLAG_LABELS.{code} is missing {field[:-1]!r}"
        sev = re.search(r'sev:\s*"(\w+)"', body)
        assert sev and sev.group(1) in _VALID_FLAG_SEV, (
            f"FLAG_LABELS.{code} has an unknown sev"
        )
        group = re.search(r'group:\s*"(\w+)"', body)
        assert group and group.group(1) in _VALID_FLAG_GROUP, (
            f"FLAG_LABELS.{code} has an unknown group"
        )
        # sx is a safety-rule code "S#" or explicit null (assist/config flags).
        assert re.search(r'sx:\s*(?:"S\d+"|null)', body), (
            f'FLAG_LABELS.{code} sx must be "S#" or null'
        )


@pytest.mark.unit
def test_every_knob_has_label_and_tooltip_in_both_languages() -> None:
    """Each exposed knob has tune_<name> and tip_knob_<name> in pl AND en."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    knobs = _knob_names()
    assert len(knobs) == 21, f"expected 21 exposed knobs, parsed {knobs}"
    missing: list[str] = []
    for knob in knobs:
        for key in (f"tune_{knob}", f"tip_knob_{knob}"):
            for lang in ("pl", "en", "de"):
                if key not in sections[lang]:
                    missing.append(f"{lang}:{key}")
    assert not missing, f"missing STR keys: {missing}"


def _knob_groups(source: str) -> list[tuple[str, list[str]]]:
    """Parse the panel's KNOB_GROUPS table into (labelKey, [knobs]) pairs.

    Args:
        source: The full JS module text.

    Returns:
        One tuple per group, in declaration order.
    """
    start = source.find("const KNOB_GROUPS = [")
    assert start >= 0, "KNOB_GROUPS not found in the panel module"
    end = source.find("\n];", start)
    assert end >= 0, "unterminated KNOB_GROUPS block"
    block = source[start:end]
    groups: list[tuple[str, list[str]]] = []
    entry_re = re.compile(r"labelKey:\s*\"(\w+)\",\s*knobs:\s*\[([^\]]*)\]", re.DOTALL)
    for match in entry_re.finditer(block):
        label_key = match.group(1)
        knobs = re.findall(r"\"(\w+)\"", match.group(2))
        groups.append((label_key, knobs))
    assert groups, "no KNOB_GROUPS entries parsed"
    return groups


@pytest.mark.unit
def test_knob_groups_cover_every_knob_exactly_once() -> None:
    """A4: every exposed knob sits in EXACTLY one panel tuning group."""
    groups = _knob_groups(_PANEL_JS.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for label_key, knobs in groups:
        for knob in knobs:
            assert knob not in seen, (
                f"knob {knob} listed in both {seen[knob]} and {label_key}"
            )
            seen[knob] = label_key
    exposed = set(_knob_names())
    grouped = set(seen)
    assert grouped == exposed, (
        f"KNOB_GROUPS vs const.py mismatch — ungrouped: "
        f"{sorted(exposed - grouped)}; unknown: {sorted(grouped - exposed)}"
    )


@pytest.mark.unit
def test_knob_group_labels_exist_in_both_languages() -> None:
    """A4: every KNOB_GROUPS labelKey resolves in STR.pl AND STR.en."""
    source = _PANEL_JS.read_text(encoding="utf-8")
    sections = _str_language_keys(source)
    for label_key, _knobs in _knob_groups(source):
        for lang in ("pl", "en", "de"):
            assert label_key in sections[lang], f"missing STR.{lang}.{label_key}"


# New v0.8.0 surfaces: the quiet-hours column, the assist group header, the
# confirmation popover and the whole heat-pump tab. Each key must exist in
# BOTH languages (the pl/en parity test already enforces the pairing; this
# list pins the keys themselves so a rename cannot silently drop a surface).
_REQUIRED_V080_KEYS = (
    "tab_hp",
    "confirm_state_live",
    "confirm_state_off",
    "confirm_mode",
    "confirm_yes",
    "confirm_cancel",
    "th_assist_group",
    "assist_no_source",
    "tip_th_assist",
    "tip_assist_target",
    "ast_th_hours",
    "ast_hours_always",
    "assist_window_sub",
    "tip_ast_hours",
    "sec_wiring",
    "wire_source_tip",
    "hp_empty",
    "hp_sec_mode",
    "hp_tortoise_mode",
    "hp_current_option",
    "hp_desired_option",
    "hp_in_sync",
    "hp_diverged",
    "hp_dhw_only_note",
    "hp_no_force",
    "tip_hp_mode",
    "hp_sec_dhw",
    "hp_dhw_switch",
    "hp_dhw_warning",
    "tip_hp_dhw",
    "hp_sec_setpoints",
    "hp_cool_target",
    "hp_cool_calc",
    "hp_heat_target",
    "hp_heat_calc",
    "hp_not_written",
    "hp_not_configured_entity",
    "tip_hp_cool",
    "tip_hp_heat",
    "hp_active_cap",
    "hp_active_unknown",
    "tip_hp_active",
    "hp_writes_paused",
)


@pytest.mark.unit
def test_v080_surfaces_have_their_str_keys() -> None:
    """The v0.8.0 UI surfaces keep their STR keys in both languages."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    missing = [
        f"{lang}:{key}"
        for key in _REQUIRED_V080_KEYS
        for lang in ("pl", "en", "de")
        if key not in sections[lang]
    ]
    assert not missing, f"missing STR keys: {missing}"


# New v0.9.0 surfaces: the S6 flow-health chip, the actuation self-test button
# and its status labels, plus the flow-watchdog knob group (issue #4). Pinned
# so a rename cannot silently drop a flow/self-test surface.
_REQUIRED_V090_KEYS = (
    "tune_grp_flow",
    "tip_flow_chip",
    "tip_actuation_test",
    "val_th_flow",
    "val_th_test",
    "flow_ok",
    "flow_no_flow",
    "test_btn_start",
    "test_btn_cancel",
    "test_running_min",
    "test_passed",
    "test_failed",
    "test_aborted",
    "test_untested",
)


@pytest.mark.unit
def test_v090_surfaces_have_their_str_keys() -> None:
    """The v0.9.0 S6 / self-test UI surfaces keep their STR keys in both langs."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    missing = [
        f"{lang}:{key}"
        for key in _REQUIRED_V090_KEYS
        for lang in ("pl", "en", "de")
        if key not in sections[lang]
    ]
    assert not missing, f"missing STR keys: {missing}"


# New v0.10.0 surface: the flag annunciator (Rooms tab) — its title, the
# collective info bubble, the four group headers and the summary/fallback
# strings. Pinned so a rename cannot silently drop the annunciator.
_REQUIRED_V0100_KEYS = (
    "flag_legend_title",
    "flag_legend_info",
    "flag_grp_safety",
    "flag_grp_assist",
    "flag_grp_config",
    "flag_grp_other",
    "flag_active_in",
    "flag_active",
    "flag_none_active",
    "flag_desc_unknown",
)


@pytest.mark.unit
def test_v0100_surfaces_have_their_str_keys() -> None:
    """The v0.10.0 flag-annunciator STR keys exist in both languages."""
    sections = _str_language_keys(_PANEL_JS.read_text(encoding="utf-8"))
    missing = [
        f"{lang}:{key}"
        for key in _REQUIRED_V0100_KEYS
        for lang in ("pl", "en", "de")
        if key not in sections[lang]
    ]
    assert not missing, f"missing STR keys: {missing}"


@pytest.mark.unit
def test_translation_jsons_have_identical_key_sets() -> None:
    """strings.json ≡ translations/en.json ≡ pl.json ≡ de.json (key sets)."""
    strings = _flatten_keys(json.loads(_STRINGS.read_text(encoding="utf-8")))
    en = _flatten_keys(
        json.loads((_TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    )
    pl = _flatten_keys(
        json.loads((_TRANSLATIONS / "pl.json").read_text(encoding="utf-8"))
    )
    de = _flatten_keys(
        json.loads((_TRANSLATIONS / "de.json").read_text(encoding="utf-8"))
    )
    assert strings == en, (
        f"strings.json vs en.json: only-strings={sorted(strings - en)}, "
        f"only-en={sorted(en - strings)}"
    )
    assert strings == pl, (
        f"strings.json vs pl.json: only-strings={sorted(strings - pl)}, "
        f"only-pl={sorted(pl - strings)}"
    )
    assert strings == de, (
        f"strings.json vs de.json: only-strings={sorted(strings - de)}, "
        f"only-de={sorted(de - strings)}"
    )
