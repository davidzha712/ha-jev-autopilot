"""Files that must agree with each other, checked so they cannot drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
PKG = ROOT / "custom_components" / "jev_autopilot"


def _keys(tree: Any, prefix: str = "") -> set[str]:
    if not isinstance(tree, dict):
        return {prefix}
    out: set[str] = set()
    for key, value in tree.items():
        out |= _keys(value, f"{prefix}.{key}" if prefix else key)
    return out


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_translations_match_strings() -> None:
    strings = _keys(_load(PKG / "strings.json"))
    for lang in ("en", "zh-Hans"):
        assert _keys(_load(PKG / "translations" / f"{lang}.json")) == strings, lang


def test_every_entity_has_an_icon() -> None:
    names = _load(PKG / "strings.json")["entity"]
    icons = _load(PKG / "icons.json")["entity"]
    for platform, entities in names.items():
        assert set(entities) == set(icons[platform]), platform


def test_requirement_pinned_once() -> None:
    manifest = _load(PKG / "manifest.json")
    tests = (ROOT / "requirements-test.txt").read_text(encoding="utf-8").splitlines()
    for req in manifest["requirements"]:
        assert req in tests, req


def test_hacs_minimum_is_a_release() -> None:
    hacs = _load(ROOT / "hacs.json")
    assert hacs["name"] == _load(PKG / "manifest.json")["name"]
    assert hacs["content_in_root"] is False
