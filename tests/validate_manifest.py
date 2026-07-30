#!/usr/bin/env python3
"""Validate manifest.json against the rules omarchy-shell actually enforces.

Mirrors PluginRegistry.validateManifest plus the parts of omarchy-plugin-validate
that matter to us, so CI fails here rather than silently on a user's machine.

Run: python tests/validate_manifest.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"

REQUIRED = ("id", "name", "version", "kinds", "entryPoints")
KINDS = {"bar-widget", "panel", "overlay", "menu", "service", "bar"}
SECTIONS = {"left", "center", "right"}
SCHEMA_TYPES = {"integer", "string", "boolean", "enum", "path", "multiselect"}

errors: list[str] = []


def check(condition: bool, message: str) -> bool:
    if not condition:
        errors.append(message)
    return condition


def main() -> int:
    if not MANIFEST.is_file():
        print("manifest.json is missing from the repo root — omarchy plugin add requires it there")
        return 1

    try:
        manifest = json.loads(MANIFEST.read_text())
    except json.JSONDecodeError as exc:
        print(f"manifest.json is not valid JSON: {exc}")
        return 1

    check(isinstance(manifest, dict), "manifest must be a JSON object")
    check(manifest.get("schemaVersion") == 1, "schemaVersion must be exactly 1")

    for field in REQUIRED:
        check(field in manifest, f"missing required field '{field}'")

    plugin_id = str(manifest.get("id", ""))
    check(bool(plugin_id), "id must be non-empty")
    check("/" not in plugin_id, "id must not contain '/'")
    check(".." not in plugin_id, "id must not contain '..'")
    check(not plugin_id.startswith("/"), "id must not start with '/'")
    check(
        not plugin_id.startswith("omarchy."),
        "the 'omarchy.' namespace is reserved for first-party plugins",
    )
    check("." in plugin_id, "third-party ids should be namespaced, e.g. 'author.widget'")

    kinds = manifest.get("kinds")
    if check(isinstance(kinds, list) and bool(kinds), "kinds must be a non-empty array"):
        for kind in kinds:
            check(kind in KINDS, f"unknown kind '{kind}' (expected one of {sorted(KINDS)})")

    entry_points = manifest.get("entryPoints")
    if check(isinstance(entry_points, dict), "entryPoints must be an object"):
        for key, value in entry_points.items():
            if not check(
                isinstance(value, str) and bool(value), f"entryPoints.{key} must be a non-empty string"
            ):
                continue
            check(not value.startswith("/"), f"entryPoints.{key} must be a relative path")
            check(".." not in value, f"entryPoints.{key} must not escape the plugin directory")
            # The shell resolves entry points relative to the plugin dir; if the
            # file is absent the widget loads as a blank slot with no error UI.
            check((ROOT / value).is_file(), f"entryPoints.{key} points at a missing file: {value}")

    bar_widget = manifest.get("barWidget")
    if isinstance(bar_widget, dict):
        section = bar_widget.get("defaultSection")
        if section is not None:
            check(
                str(section) in SECTIONS,
                f"barWidget.defaultSection must be one of {sorted(SECTIONS)}",
            )

        defaults = bar_widget.get("defaults", {})
        check(isinstance(defaults, dict), "barWidget.defaults must be an object")

        schema = bar_widget.get("schema", [])
        if check(isinstance(schema, list), "barWidget.schema must be an array"):
            seen: set[str] = set()
            for index, entry in enumerate(schema):
                where = f"barWidget.schema[{index}]"
                if not check(isinstance(entry, dict), f"{where} must be an object"):
                    continue
                key = entry.get("key")
                check(isinstance(key, str) and bool(key), f"{where}.key must be a non-empty string")
                check(key not in seen, f"{where}.key '{key}' is duplicated")
                seen.add(key)
                check(
                    entry.get("type") in SCHEMA_TYPES,
                    f"{where}.type '{entry.get('type')}' is not one of {sorted(SCHEMA_TYPES)}",
                )
                check(bool(entry.get("label")), f"{where}.label is required for the settings UI")

                if entry.get("type") == "enum":
                    options = entry.get("options")
                    check(
                        isinstance(options, list) and bool(options),
                        f"{where}.options must be a non-empty array for an enum",
                    )
                    if isinstance(options, list) and "defaultValue" in entry:
                        check(
                            entry["defaultValue"] in options,
                            f"{where}.defaultValue is not one of its own options",
                        )

                # A default declared in the schema but absent from `defaults` means
                # the widget and the settings panel disagree about the initial value.
                if "defaultValue" in entry and isinstance(defaults, dict) and key in defaults:
                    check(
                        defaults[key] == entry["defaultValue"],
                        f"{where}.defaultValue disagrees with barWidget.defaults['{key}']",
                    )

            declared = {e.get("key") for e in schema if isinstance(e, dict)}
            for key in defaults:
                check(key in declared, f"barWidget.defaults['{key}'] has no matching schema entry")

    if errors:
        print(f"manifest.json: {len(errors)} problem(s)\n")
        for message in errors:
            print(f"  - {message}")
        return 1

    print(f"manifest.json ok — id={plugin_id} kinds={manifest.get('kinds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
