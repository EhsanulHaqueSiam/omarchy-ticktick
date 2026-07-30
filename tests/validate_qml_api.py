#!/usr/bin/env python3
"""Check every Omarchy singleton member the QML touches actually exists.

`qmllint` cannot do this. `Style.spacing` and `Style.font` are declared as inline
`QtObject`s, so the linter reports "Member not found on type QObject" for a perfectly
good member and for a typo alike — which makes its output useless for exactly the
mistakes that are easiest to make. A misspelt `Style.spacing.padding` then costs
nothing at load time and silently renders as 0, i.e. a layout that is subtly wrong on
someone else's machine and fine on yours.

So the member lists live here, transcribed from the shell source. When an Omarchy
install is present the lists are re-derived from it and any drift is reported, which
keeps this file honest without making CI depend on Omarchy being installed.

Run: python tests/validate_qml_api.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
QML_DIR = ROOT / "qml"
SHELL = pathlib.Path("/usr/share/omarchy/shell")

# Transcribed from Commons/Style.qml, Commons/Color.qml, Commons/Util.qml,
# Commons/Border.qml on Omarchy (quickshell 0.3.x).
STYLE_SPACING = {
    "scale", "hairline", "xxs", "xs", "sm", "md", "lg", "xl", "xxl", "xxxl", "huge",
    "controlGap", "controlPaddingX", "controlPaddingY", "inputPaddingY", "controlHeight",
    "popupRowHeight", "dropdownWidth", "searchableDropdownWidth", "numberFieldWidth",
    "searchablePopupMinHeight", "rowGap", "rowPaddingX", "labelGap", "panelGap",
    "panelPadding", "popupPadding",
}
STYLE_FONT = {
    "family", "resolvedFamily", "menuFamily", "baseSize", "caption", "bodySmall", "body",
    "subtitle", "title", "heading", "display", "displayLarge", "iconSmall", "icon", "iconLarge",
}
STYLE_BAR = {"sizeHorizontal", "sizeVertical", "iconSlot", "iconCanvas", "iconFont", "statusSlot"}
STYLE_TOP = {"spacing", "font", "bar", "space", "spaceReal", "cornerRadius", "gapsOut",
             "fontFamily", "fontBaseSize", "spacingScale"}

COLOR_MEMBERS = {
    "foreground", "background", "accent", "urgent", "muted",
    "home", "stateHome", "currentThemePath",
    "bar", "popups", "tooltip", "notifications", "menu", "polkit", "lock", "imagePicker",
    "pick", "pickAlpha", "flatColor", "composed",
}
UTIL_MEMBERS = {
    "clamp", "clampAlpha", "wheelSteps", "alpha", "fileUrl", "shellQuote", "execDetached",
    "isPlainObject", "canonicalWidgetId", "decodeBase64", "cloneJson", "parseModuleJson",
    "editsFilter", "editedFilter",
}
BORDER_MEMBERS = {
    "none", "flat", "controlSpec", "surfaceSpec", "localOrSurfaceSpec", "withWidth",
    "isNone", "needsOverlay", "canUseNative", "top", "right", "bottom", "left",
    "uniformWidth", "color", "hyprlandActiveSpec",
}
#: Anything else silently renders as the `normal` state — no error, no visual.
CONTROL_STATES = {"normal", "hover", "hot", "hover-cursor", "selected", "focus"}

problems: list[str] = []


def scan() -> None:
    for path in sorted(QML_DIR.glob("*.qml")):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        check(rel, src, r"Style\.spacing\.(\w+)", STYLE_SPACING, "Style.spacing")
        check(rel, src, r"Style\.font\.(\w+)", STYLE_FONT, "Style.font")
        check(rel, src, r"Style\.bar\.(\w+)", STYLE_BAR, "Style.bar")
        check(rel, src, r"Style\.(\w+)", STYLE_TOP, "Style")
        check(rel, src, r"Color\.(\w+)", COLOR_MEMBERS, "Color")
        check(rel, src, r"Util\.(\w+)", UTIL_MEMBERS, "Util")
        check(rel, src, r"Border\.(\w+)", BORDER_MEMBERS, "Border")
        for match in re.finditer(r'Border\.controlSpec\(\s*"([^"]*)"', src):
            if match.group(1) not in CONTROL_STATES:
                problems.append(
                    f"{rel}: Border.controlSpec(\"{match.group(1)}\") is not a known state — "
                    f"it will silently render as 'normal' (expected one of "
                    f"{', '.join(sorted(CONTROL_STATES))})"
                )
        # There is no Icons singleton anywhere in Commons; glyphs are inline strings.
        if re.search(r"\bIcons\.", src):
            problems.append(f"{rel}: `Icons` is not an Omarchy singleton — inline the glyph")


def check(rel: pathlib.Path, src: str, pattern: str, allowed: set[str], label: str) -> None:
    for match in re.finditer(pattern, src):
        if match.group(1) not in allowed:
            problems.append(f"{rel}: {label}.{match.group(1)} does not exist")


def cross_check() -> None:
    """Re-derive the lists from a real install, so this file cannot rot unnoticed."""
    style = SHELL / "Commons" / "Style.qml"
    if not style.is_file():
        print("  (no Omarchy install here — skipping the cross-check)")
        return
    src = style.read_text(encoding="utf-8")
    for block, expected, label in (
        (_block(src, "spacing"), STYLE_SPACING, "Style.spacing"),
        (_block(src, "font"), STYLE_FONT, "Style.font"),
        (_block(src, "bar"), STYLE_BAR, "Style.bar"),
    ):
        found = set(re.findall(r"readonly\s+property\s+\w+\s+(\w+)\s*:", block))
        missing = found - expected
        if missing:
            problems.append(
                f"tests/validate_qml_api.py is out of date: {label} also has "
                f"{', '.join(sorted(missing))}"
            )
        gone = expected - found
        if gone:
            problems.append(
                f"tests/validate_qml_api.py lists {label} members this install does not "
                f"have: {', '.join(sorted(gone))}"
            )


def _block(src: str, name: str) -> str:
    """The body of `readonly property QtObject <name>: QtObject { … }`."""
    start = src.find(f"property QtObject {name}:")
    if start < 0:
        return ""
    depth, index = 0, src.find("{", start)
    if index < 0:
        return ""
    for pos in range(index, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[index:pos]
    return ""


def main() -> int:
    if not QML_DIR.is_dir():
        print("no qml/ directory")
        return 1
    scan()
    cross_check()
    if problems:
        print(f"QML API check: {len(problems)} problem(s)\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    files = len(list(QML_DIR.glob("*.qml")))
    print(f"QML API check ok — {files} file(s), every Style/Color/Util/Border member exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
