#!/usr/bin/env bash
# Render the README's screenshots from a real Quickshell, against the fake account.
#
# Real pixels, not a mockup: the widget runs in an actual layer-shell bar with the
# actual Omarchy theme, so the images cannot drift from the code the way a hand-drawn
# diagram does. The account behind them is `tests/fake_ticktick.py`, never yours —
# a screenshot of a real TickTick account in a public repo is a data leak.
#
# Needs a live Wayland session, an Omarchy install, grim and ImageMagick, so CI cannot
# run it. Run it by hand when the UI changes.
#
# Usage: tests/screenshots.sh [outdir]     (default docs/img)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHELL_DIR="${OMARCHY_PATH:-/usr/share/omarchy}/shell"
OUT="${1:-$ROOT/docs/img}"

fail() { printf '\033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
info() { printf '\033[2m  %s\033[0m\n' "$1"; }

for tool in quickshell grim magick python3; do
  command -v "$tool" >/dev/null || fail "$tool is not installed"
done
[ -d "$SHELL_DIR/Ui" ] || fail "no Omarchy shell at $SHELL_DIR (set OMARCHY_PATH)"
[ -n "${WAYLAND_DISPLAY:-}" ] || fail "no Wayland session — this needs a running compositor"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${FAKE_PID:-}" ] && kill "$FAKE_PID" 2>/dev/null' EXIT
mkdir -p "$OUT"

ln -s "$SHELL_DIR/Ui" "$WORK/Ui"
ln -s "$SHELL_DIR/Commons" "$WORK/Commons"
ln -s "$ROOT/qml" "$WORK/plugin"
ln -s "$ROOT/bin" "$WORK/bin"
mkdir -p "$WORK/config/ticktick" "$WORK/state"

info "starting the fake TickTick API…"
python3 - "$WORK" "$ROOT" <<'PY' > "$WORK/fake.log" 2>&1 &
import json, pathlib, sys, time
sys.path.insert(0, sys.argv[2])
from tests.fake_ticktick import Server, TOKEN
from datetime import datetime, timedelta

work = pathlib.Path(sys.argv[1])
s = Server(); s.__enter__()
f = s.fake

def stamp(days=0, hour=9):
    when = datetime.now().astimezone().replace(
        hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000%z")

# A believable week: two lists in a folder, one loose, and the Inbox.
f.projects = [
    {"id": "p-uni", "name": "University work", "color": "#F5A97F", "sortOrder": 1,
     "viewMode": "list", "kind": "TASK", "permission": "write", "groupId": "g-school"},
    {"id": "p-study", "name": "Study", "color": "#A6DA95", "sortOrder": 2,
     "viewMode": "list", "kind": "TASK", "permission": "write", "groupId": "g-school"},
    {"id": "p-personal", "name": "Personal", "color": "#8AADF4", "sortOrder": 3,
     "viewMode": "list", "kind": "TASK", "permission": "write"},
]
f.groups = [{"id": "g-school", "name": "University", "sortOrder": 1}]

f.add_task(title="Renew passport", projectId=f.inbox_id, priority=5, dueDate=stamp(-3, 9))
f.add_task(title="Call the bank", projectId=f.inbox_id, dueDate=stamp(0, 14))
f.add_task(title="Submit lab report", projectId="p-uni", priority=3, dueDate=stamp(0, 18),
           content="Include the calibration table and the error analysis.",
           reminders=["TRIGGER:-PT30M"],
           items=[{"id": "i1", "title": "plot the results", "status": 1},
                  {"id": "i2", "title": "write the discussion", "status": 0},
                  {"id": "i3", "title": "proofread", "status": 0}])
f.add_task(title="Meeting notes", projectId="p-uni", kind="NOTE", dueDate=stamp(0, 11),
           content="Supervisor wants the draft by Friday.")
f.add_task(title="Read chapter 4", projectId="p-study", dueDate=stamp(0, 21), tags=["errand"])
f.add_task(title="Groceries", projectId="p-personal", dueDate=stamp(0, 19))
f.add_task(title="Book flights", projectId="p-personal", isAllDay=True, dueDate=stamp(9, 0),
           repeatFlag="RRULE:FREQ=YEARLY;INTERVAL=1")
f.add_task(title="Pack for trip", projectId="p-personal", isAllDay=True, dueDate=stamp(1, 0))
f.add_task(title="Someday idea", projectId=f.inbox_id)

(work / "base.txt").write_text(s.base)
(work / "config/ticktick/credentials.json").write_text(
    json.dumps({"access_token": TOKEN, "inbox_id": f.inbox_id}))
print("ready", s.base, flush=True)
time.sleep(240)
PY
FAKE_PID=$!

for _ in $(seq 1 50); do [ -s "$WORK/base.txt" ] && break; sleep 0.2; done
[ -s "$WORK/base.txt" ] || { cat "$WORK/fake.log"; fail "fake API never came up"; }
info "fake API at $(cat "$WORK/base.txt")"

# A real bar strip, so KeyboardPanel anchors and positions the way it does in Omarchy.
cat > "$WORK/shell.qml" <<'QML'
import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "plugin" as Plugin

ShellRoot {
  id: shell

  property var widget: null
  property string shot: ""

  // A slice of the bar around the widget: the button on its own is 27px wide and
  // says nothing. In context you can see it is a bar widget with a live count.
  function announceBar(name, item) {
    if (!item) return
    var p = item.mapToItem(null, 0, 0)
    var lead = 230
    console.log("SHOT " + name + " " + Math.round(Math.max(0, p.x - lead)) + " 0 "
                + Math.round(item.width + lead + 12) + " " + Math.round(barWindow.height))
  }

  // The popup is a full-screen layer surface with the card drawn inside it at
  // `cardOrigin`, so the card's rect is the thing worth cropping to.
  function announceCard(name) {
    var p = shell.widget ? shell.widget.panel : null
    if (!p) return
    // The card is exactly contentWidth x contentHeight; `padding` is an inset
    // applied inside it, not an outset, so adding it drags in the wallpaper.
    console.log("SHOT " + name + " " + Math.round(p.cardOrigin.x) + " " + Math.round(p.cardOrigin.y)
                + " " + Math.round(p.contentWidth) + " " + Math.round(p.contentHeight))
  }

  PanelWindow {
    id: barWindow
    anchors { top: true; left: true; right: true }
    implicitHeight: Style.bar.sizeHorizontal
    color: Color.bar.background
    // Overlay, not Top: the machine's real Omarchy bar is also a Top surface at the
    // same edge, and it would otherwise be the one that ends up in the screenshot —
    // which is both the wrong widget and somebody's actual tray. -1 opts out of the
    // real bar's exclusive zone as well, which would otherwise push this one 24px
    // down the screen and leave the crop pointing straight at it.
    exclusiveZone: -1
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.namespace: "shot-bar"

    QtObject {
      id: fakeBar
      property string position: "top"
      property string fontFamily: Style.font.family
      property color foreground: Color.foreground
      property color barForeground: Color.bar.text
      property color background: Color.bar.background
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
      function moduleWidgets(id) { return [subject] }
    }

    Row {
      anchors.right: parent.right
      anchors.rightMargin: Style.spacing.md
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.spacing.md

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: "󰃰 14:32   󰂄 87%"
        color: Qt.darker(Color.bar.text, 1.4)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Plugin.BarWidget {
        id: subject
        anchors.verticalCenter: parent.verticalCenter
        bar: fakeBar
        moduleName: "siam.ticktick"
        settings: ({ refreshIntervalSec: 86400, badgeMode: "Today", hideWhenEmpty: "false",
                     showProjectChips: "true", confirmDelete: "true", includeUndated: "true",
                     sortBy: "List" })
        Component.onCompleted: shell.widget = subject
      }
    }
  }

  // One capture per step; the shell script grabs the screen between them.
  Timer {
    id: script
    property int step: 0
    interval: 2600
    repeat: true
    running: true
    onTriggered: {
      var w = shell.widget
      if (!w) return
      switch (step) {
      case 0:
        shell.announceBar("bar", w)
        console.log("SHOT ready badge=" + w.badgeText + " rows=" + w.rowCount)
        break
      case 1:
        w.open()
        break
      case 2:
        shell.announceCard("popup")
        console.log("SHOT step popup")
        break
      case 3:
        // The lab report: a priority, a note beneath it, a checklist and a due time.
        for (var i = 0; i < w.rows.length; i++) {
          if (String(w.rows[i].title).indexOf("lab report") >= 0) {
            w.setCursor(i)
            w.toggleExpanded(w.rows[i].id)
          }
        }
        break
      case 4:
        shell.announceCard("detail")
        console.log("SHOT step detail")
        break
      case 5:
        w.expandedId = ""
        w.setView("project")
        break
      case 6:
        shell.announceCard("lists")
        console.log("SHOT step lists")
        break
      default:
        Qt.quit()
      }
      step += 1
    }
  }
}
QML

info "rendering…"
XDG_CONFIG_HOME="$WORK/config" \
XDG_STATE_HOME="$WORK/state" \
TICKTICK_API_BASE="$(cat "$WORK/base.txt")" \
  quickshell -n -p "$WORK/shell.qml" > "$WORK/out.log" 2>&1 &
QS_PID=$!

capture() {  # capture <name> <ready-marker>
  local name="$1" marker="$2" rect="" tries=0
  while [ $tries -lt 60 ]; do
    if grep -q "SHOT $marker" "$WORK/out.log" 2>/dev/null; then break; fi
    sleep 0.25; tries=$((tries + 1))
  done
  grep -q "SHOT $marker" "$WORK/out.log" || { info "no $marker marker; skipping $name"; return; }
  sleep 0.5
  # The geometry line is named after the image, not after the readiness marker.
  rect="$(sed -e 's/\x1b\[[0-9;]*m//g' "$WORK/out.log" \
          | grep -oE "SHOT $name [0-9]+ [0-9]+ [0-9]+ [0-9]+" | tail -1)"
  grim "$WORK/$name-full.png" 2>/dev/null || { info "grim failed for $name"; return; }
  if [ -n "$rect" ]; then
    # `SHOT <name> x y w h` — five fields after the marker word.
    set -- $rect
    # Logical coordinates; grim writes physical pixels, so scale the crop box.
    python3 - "$WORK/$name-full.png" "$OUT/$name.png" "$3" "$4" "$5" "$6" <<'PY'
import subprocess, sys
src, dst, x, y, w, h = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:7])
out = subprocess.run(["magick", "identify", "-format", "%w %h", src],
                     capture_output=True, text=True).stdout.split()
pw, ph = int(out[0]), int(out[1])
# The logical screen is whatever the compositor reports; derive the scale from the
# ratio rather than trusting a hard-coded number.
import json
mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"],
                                 capture_output=True, text=True).stdout)
lw = max(int(m["width"] / m["scale"]) for m in mons)
scale = pw / lw
# Exactly the widget, nothing of the desktop behind it: any padding would drag in
# the wallpaper and the real bar, which have no business in this repo's README.
box = (max(0, int(x * scale)), max(0, int(y * scale)),
       min(pw, int(w * scale)), min(ph, int(h * scale)))
# The card is drawn with rounded corners over whatever is behind it, so the four
# corners are wallpaper. Mask them to transparent — which also reads correctly on
# GitHub in both light and dark themes.
# The popup card has rounded corners; the bar strip is a square slice of a full-width
# surface, so rounding it would eat pixels that are really there.
radius = max(4, int(10 * scale)) if h > 60 else 0
subprocess.run([
    "magick", src, "-crop", f"{box[2]}x{box[3]}+{box[0]}+{box[1]}", "+repage",
    "(", "+clone", "-alpha", "extract", "-draw",
    f"fill black polygon 0,0 0,{radius} {radius},0 "
    f"fill white circle {radius},{radius} {radius},0", "(", "+clone", "-flip", ")",
    "-compose", "multiply", "-composite", "(", "+clone", "-flop", ")",
    "-compose", "multiply", "-composite", ")",
    # Written at the display's physical resolution rather than downscaled: the README
    # sizes them with `width`, so a 2x source is what keeps them crisp on a HiDPI screen.
    "-alpha", "off", "-compose", "copy_opacity", "-composite", dst,
], check=True)
print(f"  wrote {dst}")
PY
  else
    # Never fall back to the whole screen: these images go in a public README, and a
    # full-screen grab of a developer's desktop is both off-topic and a data leak.
    fail "no geometry for $name — refusing to write a full-screen capture"
  fi
}

capture bar "bar "
capture popup "step popup"
capture detail "step detail"
capture lists "step lists"

wait $QS_PID 2>/dev/null
sed -e 's/\x1b\[[0-9;]*m//g' "$WORK/out.log" | grep -E "SHOT|qml:" | sed 's/^/  /'

ls -1 "$OUT"/*.png 2>/dev/null | sed 's/^/  /' || fail "nothing was captured"
printf '\033[32mOK\033[0m   screenshots in %s\n' "$OUT"
