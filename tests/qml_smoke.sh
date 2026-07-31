#!/usr/bin/env bash
# Load the widget in a real Quickshell and fail on any QML error or warning.
#
# This is the only check that exercises the QML at runtime. `qmllint` parses and
# resolves types, but it will not tell you that `function escape()` is an illegal
# method name, or that a wrapping Text bound to `height: implicitHeight` spins in a
# binding loop. Both of those shipped past qmllint and were caught here.
#
# It needs a live Wayland session and an Omarchy install, so CI cannot run it —
# run it on the box before releasing. Everything it touches is a temp directory;
# the running bar and the real credential store are never involved.
#
# Usage: tests/qml_smoke.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHELL_DIR="${OMARCHY_PATH:-/usr/share/omarchy}/shell"

fail() { printf '\033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
info() { printf '\033[2m  %s\033[0m\n' "$1"; }

command -v quickshell >/dev/null || fail "quickshell is not installed"
[ -d "$SHELL_DIR/Ui" ] || fail "no Omarchy shell at $SHELL_DIR (set OMARCHY_PATH)"
[ -n "${WAYLAND_DISPLAY:-}" ] || fail "no Wayland session — this test needs a running compositor"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${FAKE_PID:-}" ] && kill "$FAKE_PID" 2>/dev/null' EXIT

# A shell root the engine can resolve `qs.Ui` / `qs.Commons` from, with the plugin
# mounted where its own `../bin/ticktick` lookup expects to find the helper.
ln -s "$SHELL_DIR/Ui" "$WORK/Ui"
ln -s "$SHELL_DIR/Commons" "$WORK/Commons"
ln -s "$ROOT/qml" "$WORK/plugin"
ln -s "$ROOT/bin" "$WORK/bin"

mkdir -p "$WORK/config/ticktick" "$WORK/state"

info "starting the fake TickTick API…"
python3 - "$WORK" "$ROOT" <<'PY' > "$WORK/fake.log" 2>&1 &
import json, pathlib, sys, time
sys.path.insert(0, sys.argv[2])  # the repo root, so `tests.fake_ticktick` imports
from tests.fake_ticktick import Server, TOKEN
from datetime import datetime, timedelta

work = pathlib.Path(sys.argv[1])
s = Server(); s.__enter__()
f = s.fake

# Relative to the clock, never absolute: a fixture pinned to a literal date passes
# on the day it was written and silently changes bucket every day after.
def stamp(days=0, hour=9):
    when = datetime.now().astimezone().replace(
        hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000%z")

f.add_task(title="Renew domain", projectId="p-work", isAllDay=True,
           dueDate=stamp(-120, 0), priority=5,
           repeatFlag="RRULE:FREQ=YEARLY;INTERVAL=1")           # overdue
f.add_task(title="Submit report", projectId="p-work", priority=3,
           dueDate=stamp(0, 23), content="Include Q3 numbers.",
           reminders=["TRIGGER:-PT30M"],                        # today
           items=[{"id": "s1", "title": "gather figures", "status": 1},
                  {"id": "s2", "title": "write summary", "status": 0}])
f.add_task(title="Call the bank", projectId="p-home",
           dueDate=stamp(0, 23))                                # today
f.add_task(title="Pack for trip", projectId="p-home", isAllDay=True, tags=["errand"],
           dueDate=stamp(1, 0),                                 # tomorrow
           items=[{"id": "i1", "title": "passport", "status": 1},
                  {"id": "i2", "title": "charger", "status": 0}])
f.add_task(title="Someday idea", projectId=f.inbox_id)          # undated
# Due today, but a note: it renders and it must not reach the badge.
f.add_task(title="Meeting notes", projectId="p-work", kind="NOTE",
           content="What was said.", dueDate=stamp(0, 20))
(work / "base.txt").write_text(s.base)
(work / "config/ticktick/credentials.json").write_text(
    json.dumps({"access_token": TOKEN, "inbox_id": f.inbox_id}))
print("ready", s.base, flush=True)
time.sleep(180)
PY
FAKE_PID=$!

for _ in $(seq 1 50); do [ -s "$WORK/base.txt" ] && break; sleep 0.2; done
[ -s "$WORK/base.txt" ] || { cat "$WORK/fake.log"; fail "fake API never came up"; }
info "fake API at $(cat "$WORK/base.txt")"

cat > "$WORK/shell.qml" <<'QML'
import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "plugin" as Plugin

ShellRoot {
  // Everything Bar.qml exposes that a bar widget is documented to reach for.
  QtObject {
    id: fakeBar
    property string position: "top"
    property string fontFamily: Style.font.family
    property color foreground: Color.foreground
    property color barForeground: Color.foreground
    property color background: Color.background
    property color urgent: Color.urgent
    property bool foregroundAnimationEnabled: false
    property bool vertical: false
    property int barSize: Style.bar.sizeHorizontal
    property bool transparent: false
    property var activePopout: null
    property var clickTargets: []
    property var shell: null
    property string omarchyPath: "/usr/share/omarchy"
    function run(cmd) {}
    function runProcess(p) {}
    function showTooltip(t, text) {}
    function hideTooltip(t) {}
    function requestPopout(o) {}
    function releasePopout(o) {}
    function registerClickTarget(t) {}
    function unregisterClickTarget(t) {}
    function targetBelongsToWindow(t, w) { return false }
    function switchPanelFrom(o, d) { return false }
    function moduleWidgets(id) { return [] }
  }

  Plugin.BarWidget {
    id: subject
    bar: fakeBar
    moduleName: "siam.ticktick"
    // Strings on purpose: `omarchy bar plugin set` writes them that way unless the
    // caller remembers `--json`, and reading them as booleans is a real past bug.
    settings: ({ refreshIntervalSec: 86400, badgeMode: "Today", hideWhenEmpty: "false",
                 showProjectChips: "true", confirmDelete: "true", includeUndated: "true",
                 sortBy: "List" })
  }

  Timer {
    interval: 4000
    running: true
    onTriggered: {
      console.log("SMOKE geometry=" + subject.implicitWidth + "x" + subject.implicitHeight
                  + " visible=" + subject.visible + " badge=" + subject.badgeText)
      console.log("SMOKE settings hideWhenEmpty=" + subject.hideWhenEmpty
                  + " showProjectChips=" + subject.showProjectChips
                  + " confirmDelete=" + subject.confirmDelete)
      console.log("SMOKE rows=" + subject.rowCount)
      console.log("SMOKE sections=" + subject.sectionFor(0) + "|" + subject.sectionFor(1)
                  + "|" + subject.sectionFor(2))
      subject.open()
      Qt.callLater(function () {
        // Every tab, in the order the number keys walk them.
        for (var i = 0; i < subject.views.length; i++) subject.setView(subject.views[i])
        subject.cycleView(1); subject.cycleView(-1)
        subject.moveCursor(1); subject.moveCursor(-1)
        subject.openSearch(); subject.closeSearch(true)
        subject.openAdd(); subject.closeAdd()
        subject.toggleFilters(); subject.toggleFilters()
        // The four sort modes, back round to where it started.
        for (var s = 0; s < 4; s++) subject.service.cycleSort()
        subject.service.cyclePriorityFilter(); subject.service.setPriorityFilter(-1)
        subject.service.sync()
        subject.setView("today")
        // Expanding a task that has a checklist must actually grow the panel. Rows
        // reach a delegate as a QVariant, and a nested array arrives array-LIKE but
        // fails Array.isArray — which silently blanked every checklist and reminder.
        var withItems = null
        for (var w = 0; w < subject.rows.length; w++) {
          if (subject.rows[w].itemsTotal > 0) withItems = subject.rows[w]
        }
        detailProbe.shut = subject.panel.contentHeight
        detailProbe.subjectRow = withItems
        if (withItems) subject.toggleExpanded(withItems.id)
        detailProbe.running = true   // the pane is built on the next tick, not this one

        if (subject.rowCount > 0) {
          subject.setCursor(0)
          subject.toggleExpanded(subject.rows[0].id)
          subject.cyclePriority(subject.rows[0])
          subject.requestDelete(subject.rows[0])
          console.log("SMOKE confirmOpen=" + subject.confirmOpen)
          subject.cancelDelete()
        }
        // A new task must be on screen on the keystroke, not two process launches
        // and a round trip later.
        var before = subject.rowCount
        subject.service.add("smoke draft task")
        console.log("SMOKE add before=" + before + " after=" + subject.rowCount)
        // …and it must join the group it belongs to. Appended to the end it would
        // open a second heading for a list already on screen further up.
        var seen = {}, dupes = 0
        for (var r = 0; r < subject.rowCount; r++) {
          var head = subject.sectionFor(r)
          if (head === "") continue
          if (seen[head]) dupes += 1
          seen[head] = true
        }
        console.log("SMOKE dupsections=" + dupes)
        // A note has no checkbox: completing one must be refused, not queued.
        var note = null
        for (var n = 0; n < subject.rows.length; n++) {
          if (subject.rows[n].isNote) note = subject.rows[n]
        }
        console.log("SMOKE note=" + (note ? note.title : "none"))
        if (note) subject.completeAt(note)
        subject.dismiss()
        console.log("SMOKE done rows=" + subject.rowCount + " sort=" + subject.service.sort)
        quitTimer.running = true
      })
    }
  }
  Timer {
    id: detailProbe
    property real shut: 0
    property var subjectRow: null
    interval: 500
    onTriggered: {
      console.log("SMOKE detail shut=" + Math.round(shut)
                  + " open=" + Math.round(subject.panel.contentHeight)
                  + " items=" + (subjectRow ? subjectRow.itemsTotal : 0))
      if (subjectRow) subject.toggleExpanded(subjectRow.id)
    }
  }

  Timer { id: quitTimer; interval: 2500; onTriggered: Qt.quit() }
}
QML

info "running the widget in quickshell…"
XDG_CONFIG_HOME="$WORK/config" \
XDG_STATE_HOME="$WORK/state" \
TICKTICK_API_BASE="$(cat "$WORK/base.txt")" \
  timeout 45 quickshell -n -p "$WORK/shell.qml" > "$WORK/out.log" 2>&1
status=$?

sed -e 's/\x1b\[[0-9;]*m//g' "$WORK/out.log" > "$WORK/clean.log"

grep -E "^\s*(SMOKE|.*qml:)" "$WORK/clean.log" | sed 's/^/  /'

[ $status -eq 0 ] || { cat "$WORK/clean.log"; fail "quickshell exited $status"; }

# The portal complaint is this harness running beside the real shell, not our code.
problems="$(grep -E "ERROR|WARN" "$WORK/clean.log" \
  | grep -v "Failed to register with host portal" || true)"
if [ -n "$problems" ]; then
  echo "$problems"
  fail "QML produced errors or warnings"
fi

# Today = overdue + today: "Renew domain", "Submit report", "Call the bank", plus the
# note that is also due today. Tomorrow's and the undated one are correctly excluded.
grep -q "SMOKE rows=4" "$WORK/clean.log" \
  || { cat "$WORK/clean.log"; fail "expected 4 rows (1 overdue + 2 today + 1 note) in Today"; }
# …but the badge counts only the three real tasks. A note has no checkbox, so
# counting one reports work that does not exist.
grep -q "SMOKE .*badge=3" "$WORK/clean.log" \
  || { cat "$WORK/clean.log"; fail "the badge counted the note"; }
grep -q "SMOKE note=Meeting notes" "$WORK/clean.log" \
  || fail "the note never reached the list"
grep -q "SMOKE add before=4 after=5" "$WORK/clean.log" \
  || { cat "$WORK/clean.log"; fail "a new task did not appear synchronously"; }
grep -q "SMOKE dupsections=0" "$WORK/clean.log" \
  || { cat "$WORK/clean.log"; fail "a list was headed twice — the draft opened its own section"; }
# Sorting by list means the headings are list names, not Overdue/Today.
grep -qE "SMOKE sections=(WORK|HOME|INBOX)" "$WORK/clean.log" \
  || { cat "$WORK/clean.log"; fail "sections are not headed by list name under sort=list"; }
grep -q "SMOKE done .* sort=list" "$WORK/clean.log" \
  || fail "cycling the sort four times did not return to where it started"
grep -q "SMOKE confirmOpen=true" "$WORK/clean.log" \
  || fail "the delete confirmation never opened"
grep -q "SMOKE settings hideWhenEmpty=false showProjectChips=true confirmDelete=true" \
  "$WORK/clean.log" || fail "string settings were not coerced to booleans"

printf '\033[32mOK\033[0m   QML loads, renders and drives cleanly\n'
