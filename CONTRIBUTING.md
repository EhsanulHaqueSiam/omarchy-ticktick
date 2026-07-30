# Contributing

Thanks for looking. This is a small plugin with a deliberately small dependency footprint —
please keep it that way.

## Ground rules

- **Python: standard library only.** Users install this plugin with `omarchy plugin add`, which
  is a `git clone` and nothing else. There is no `pip install` step, no virtualenv, no build.
  A third-party import is a bug report from someone whose bar went blank.
- **QML never speaks HTTP.** All network work lives in `ticktick/`. `qml/Service.qml` runs
  `bin/ticktick <subcommand>` and parses one JSON object. Keeping that boundary is what makes
  the whole thing testable from a terminal.
- **Every subcommand exits 0 and prints exactly one JSON object.** A traceback reaching stdout
  breaks the widget's parser and shows the user nothing. Errors are
  `{"ok": false, "error": "<kind>", "message": "..."}`.
- **Logic that can be tested belongs in Python**, not in QML. Date bucketing, natural-language
  parsing, grouping and sorting are all covered by `tests/`. `qml/Model.js` is presentation only.

## Development loop

```bash
git clone https://github.com/EhsanulHaqueSiam/omarchy-ticktick.git
cd omarchy-ticktick

# Helper: no setup needed
python -m unittest discover -s tests -v
python bin/ticktick selftest
python bin/ticktick parse "submit report tomorrow 5pm !high #work"

# The gates CI runs
python tests/validate_manifest.py
python tests/validate_qml_api.py

# Needs a live Omarchy session, so CI cannot run it — run it before releasing
./tests/qml_smoke.sh
```

Nothing above touches your real account: the end-to-end tests talk to a fake TickTick
served on loopback, and every test redirects the credential store and the task cache
into a temp directory. Do the same for anything you run by hand —

```bash
XDG_CONFIG_HOME=$(mktemp -d) XDG_STATE_HOME=$(mktemp -d) python bin/ticktick status
```

— because `ticktick logout` against your own config really does delete your token.

To run your working copy as the live widget, symlink it into the plugins directory:

```bash
ln -s "$PWD" ~/.config/omarchy/plugins/siam.ticktick
omarchy plugin rescan
omarchy bar plugin add siam.ticktick right
```

`omarchy plugin rescan` hot-reloads plugin code — no shell restart, no logout. Edit a `.qml`
file, rescan, and the widget rebuilds in place. If a widget slot goes blank after an edit, you
have a QML error; read it with `quickshell log "$(ls -t /run/user/$UID/quickshell/by-id/*/log.qslog | head -1)"`.

## Testing

`unittest`, no pytest, no fixtures library. Tests must be deterministic: anything touching
"now" takes an injected clock (`ticktick.dates.now()` exists as a single seam for exactly this).
No test may hit the real network — `tests/fake_ticktick.py` serves a stand-in on loopback, and
talking to it over real HTTP is deliberate: it keeps argument parsing, the retry layer, the
JSON contract, the cache and the outbox inside the test rather than stubbed out.

New parser rules need **negative** cases too — the parser's job is to leave text it does not
confidently understand alone, and that is what regresses.

### Checking QML

`qmllint` is necessary and not sufficient. It parses and resolves types, but it will not tell
you that `function escape()` is an illegal method name, that a wrapping `Text` bound to
`height: implicitHeight` spins in a binding loop, or that `Style.spacing.foo` does not exist —
`Style.spacing` is an inline `QtObject`, so the linter flags valid members and typos alike.
Both of the first two shipped past it and were caught by `tests/qml_smoke.sh`, which loads the
widget in a real Quickshell and fails on any warning. `tests/validate_qml_api.py` covers the
third, and cross-checks its own member lists against a real install when one is present.

To type-check locally, `qs.Ui` has to be reachable under a `qs/` prefix:

```bash
m=$(mktemp -d); mkdir "$m/qs"
ln -s /usr/share/omarchy/shell/Ui "$m/qs/Ui"
ln -s /usr/share/omarchy/shell/Commons "$m/qs/Commons"
/usr/lib/qt6/bin/qmllint -I "$m" -I /usr/lib/qt6/qml qml/*.qml
```

## Pull requests

- One concern per PR.
- CI must be green: tests on Python 3.11–3.13, manifest validation, the QML API check,
  and QML syntax.
- Bump `version` in **both** `manifest.json` and `ticktick/__init__.py`, and add a
  `CHANGELOG.md` entry.
- Screenshots for anything that changes the UI.

## Reporting bugs

Include the output of:

```bash
ticktick status
ticktick sync
omarchy plugin list | grep ticktick
```

`status` never prints your token, so it is safe to paste.
