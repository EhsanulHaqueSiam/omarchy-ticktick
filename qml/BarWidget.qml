import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The plugin's entry point: bar button, badge, and the keyboard-driven popup.
//
// Everything here is presentation and input. State, process orchestration and the
// optimistic write queue live in Service.qml; anything worth a unit test lives in
// Python behind bin/ticktick. This file's whole job is to turn key presses and
// clicks into Service calls, and rows into pixels.
//
// Three host contracts this file is written against, all of which bite silently
// when broken:
//
//   1. The bar injects exactly `bar`, `moduleName` and `settings`, and it does so
//      AFTER construction — every read of `bar` is guarded.
//   2. `settings` is the shell.json entry minus its id. The manifest's `defaults`
//      block is inert at runtime, so every default lives in `setting(key, fallback)`
//      calls, and values arrive as STRINGS unless the user passed `--json`.
//   3. The bar is instantiated once per monitor, but an IPC target routes to only
//      one of those instances — so every IPC mutation fans out via `bar.moduleWidgets`.
Panel {
  id: widget

  moduleName: "siam.ticktick"
  // The plugin id, not a friendly short name. An IPC target is single-occupancy —
  // the first handler to register wins and every later one is silently dead — so a
  // generic "ticktick" would break the moment any other TickTick plugin (or an older
  // copy of this one) is installed alongside it. That collision was observed live.
  ipcTarget: "siam.ticktick"
  // Panel's built-in handler only offers open/close/show/hide/toggle, and a target
  // accepts exactly one handler. Ours adds refresh/add/count, so Panel's must stand down.
  manageIpc: false

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // The data layer, reachable from outside. This file is presentation and input;
  // anything that is really a question about state — what the sort is, what is
  // queued — is answered here, and the QML smoke test drives it the same way.
  readonly property alias service: tt
  // The popup surface. Exposed for the same reason `service` is: so a harness can
  // ask where the card ended up rather than guessing at its geometry.
  readonly property alias panel: panel

  readonly property bool hideWhenEmpty: tt.boolSetting("hideWhenEmpty", false)
  readonly property bool showProjectChips: tt.boolSetting("showProjectChips", true)
  readonly property bool confirmDelete: tt.boolSetting("confirmDelete", true)

  // --- cursor / mode state ----------------------------------------------

  property bool cursorActive: false
  property int cursorIndex: 0
  property string expandedId: ""
  property bool searching: false
  property bool adding: false
  property bool filtering: false
  // The token field is the fallback, revealed only when asked for, so the signed-out
  // panel leads with the one button most people want.
  property bool pastingToken: false
  property bool detailPickerOpen: false
  property bool confirmOpen: false
  property var confirmTask: null
  // Enter emits returnRequested and THEN activateRequested. Space emits only
  // activateRequested. This flag is how the two are told apart.
  property bool _returnConsumed: false

  // TickTick's own smart lists, in its own sidebar order, plus the user's lists.
  // The number keys follow this order, so it is the one place it is written down.
  readonly property var viewOptions: [
    { value: "today", label: "Today" },
    { value: "tomorrow", label: "Tomorrow" },
    { value: "next", label: "Next 7" },
    { value: "inbox", label: "Inbox" },
    { value: "all", label: "All" },
    { value: "project", label: "Lists" },
    { value: "completed", label: "Done" }
  ]
  readonly property var views: {
    var out = []
    for (var i = 0; i < viewOptions.length; i++) out.push(viewOptions[i].value)
    return out
  }

  // The Lists tab shows the list of lists until one is picked; after that it is
  // an ordinary task list scoped to that list.
  readonly property bool pickingProject: tt.view === "project" && tt.projectFilter === ""
  readonly property var rows: pickingProject ? sortedProjects : (tt.tasks || [])
  readonly property int rowCount: rows.length

  // Folders, the way TickTick's sidebar shows them: loose lists first (the Inbox is
  // one), then each folder's lists together. A folder the catalogue does not name
  // sorts last rather than first, which an unranked 0 would have done.
  readonly property var groupRank: {
    var out = {}
    var list = tt.groups || []
    for (var i = 0; i < list.length; i++) out[String(list[i].id || "")] = i
    return out
  }

  function groupRankOf(project) {
    var gid = String((project || {}).groupId || "")
    if (gid === "") return -1
    var rank = widget.groupRank[gid]
    return rank === undefined ? 1e6 : rank
  }

  function groupNameOf(project) {
    var gid = String((project || {}).groupId || "")
    if (gid === "") return "Lists"
    var list = tt.groups || []
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].id || "") === gid) return Model.squish(list[i].name) || "Folder"
    }
    return "Folder"
  }

  readonly property var sortedProjects: {
    var list = tt.projects || []
    // Decorated with the original index so lists inside one folder keep the order
    // TickTick gave them — Array.prototype.sort is not required to be stable in
    // every JS engine, and the Inbox leading the list is not negotiable.
    var decorated = []
    for (var i = 0; i < list.length; i++) decorated.push({ project: list[i], at: i })
    decorated.sort(function (a, b) {
      var ra = widget.groupRankOf(a.project)
      var rb = widget.groupRankOf(b.project)
      return ra !== rb ? ra - rb : a.at - b.at
    })
    var out = []
    for (var j = 0; j < decorated.length; j++) out.push(decorated[j].project)
    return out
  }

  readonly property string summary: {
    if (!tt.authed) return "Not signed in"
    if (!tt.loaded) return "Loading…"
    var c = tt.counts || {}
    var over = Number(c.overdue || 0)
    var today = Number(c.today || 0)
    if (over > 0 && today > 0) return over + " overdue · " + today + " today"
    if (over > 0) return over + (over === 1 ? " task overdue" : " tasks overdue")
    if (today > 0) return today + (today === 1 ? " task today" : " tasks today")
    return "Nothing due"
  }

  readonly property string badgeText: tt.authed ? String(tt.badgeCount) : "!"

  // --- helpers ----------------------------------------------------------

  readonly property var tagOptions: {
    var out = [{ value: "", label: "Any tag" }]
    var list = tt.tags || []
    for (var i = 0; i < list.length; i++) {
      var tag = list[i] || {}
      var name = Model.squish(tag.name)
      if (name !== "") out.push({ value: name, label: "#" + (Model.squish(tag.label) || name) })
    }
    return out
  }

  function selectedTask() {
    if (pickingProject) return null
    return (cursorIndex >= 0 && cursorIndex < rowCount) ? rows[cursorIndex] : null
  }

  function selectedProject() {
    if (!pickingProject) return null
    return (cursorIndex >= 0 && cursorIndex < rowCount) ? rows[cursorIndex] : null
  }

  function clampCursor() {
    if (cursorIndex >= rowCount) cursorIndex = Math.max(0, rowCount - 1)
    if (cursorIndex < 0) cursorIndex = 0
  }

  // A keyboard move must scroll the cursor into view; a mouse hover must not. The
  // hovered row is by definition already under the pointer, and scrolling slides the
  // list out from under it — a different clipped row lands under the mouse, hovers,
  // scrolls again, and the list runs itself to the bottom on a mouse-over nobody
  // meant as a scroll. `scroll !== false` so existing one-argument callers are unaffected.
  function setCursor(index, scroll) {
    cursorActive = true
    cursorIndex = Math.max(0, Math.min(rowCount - 1, index))
    if (scroll !== false) scrollIntoView()
  }

  function moveCursor(delta) {
    cursorActive = true
    if (rowCount === 0) return
    cursorIndex = Math.max(0, Math.min(rowCount - 1, cursorIndex + delta))
    scrollIntoView()
  }

  function cycleView(step) {
    var at = views.indexOf(tt.view)
    if (at < 0) at = 0
    setView(views[(at + step + views.length) % views.length])
  }

  function setView(name) {
    tt.setProjectFilter("")
    tt.setView(name)
    cursorIndex = 0
    expandedId = ""
    if (listFlick) listFlick.contentY = 0
  }

  // The section a row opens, or "" when the row above already opened it. What the
  // sections mean follows the sort: by list they are list names (TickTick's own
  // default), by date they are Overdue / Today / Tomorrow. Sorting by priority or
  // title has no sections at all — a heading would cut across the order asked for.
  function sectionFor(index) {
    var list = rows
    if (index < 0 || index >= list.length) return ""
    if (pickingProject) {
      var name = widget.groupNameOf(list[index])
      if (index > 0 && widget.groupNameOf(list[index - 1]) === name) return ""
      return name.toUpperCase()
    }
    var key = Model.sectionKey(list[index], tt.effectiveSort)
    if (key === "") return ""
    if (index > 0 && Model.sectionKey(list[index - 1], tt.effectiveSort) === key) return ""
    return Model.sectionLabel(list[index], tt.effectiveSort).toUpperCase()
  }

  function toggleExpanded(taskId) {
    var id = String(taskId || "")
    expandedId = (expandedId === id) ? "" : id
  }

  function completeAt(task) {
    if (!task) return
    tt.complete(task)
    if (expandedId === String(task.id || "")) expandedId = ""
    Qt.callLater(widget.clampCursor)
  }

  function requestDelete(task) {
    if (!task) return
    if (!confirmDelete) {
      tt.remove(task)
      Qt.callLater(widget.clampCursor)
      return
    }
    confirmTask = task
    confirmOpen = true
  }

  function confirmDeleteNow() {
    var task = confirmTask
    confirmOpen = false
    confirmTask = null
    if (task) {
      tt.remove(task)
      if (expandedId === String(task.id || "")) expandedId = ""
      Qt.callLater(widget.clampCursor)
    }
  }

  function cancelDelete() {
    confirmOpen = false
    confirmTask = null
  }

  function cyclePriority(task) {
    if (!task) return
    // TickTick has exactly four levels; walking up and wrapping is the whole story.
    var ladder = [0, 1, 3, 5]
    var at = ladder.indexOf(Model.toInt(task.priority))
    tt.setPriority(task, ladder[(at < 0 ? 0 : at + 1) % ladder.length])
  }

  function openSearch() {
    searching = true
    Qt.callLater(function () { searchField.forceActiveFocus(); searchField.selectAll() })
  }

  function closeSearch(clearText) {
    if (clearText) {
      // The field's `text` binding is broken the moment the user types into it,
      // so clearing the service alone would leave the old query on screen.
      searchField.text = ""
      tt.setSearch("")
    }
    searching = false
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  function openAdd() {
    adding = true
    Qt.callLater(function () { quickAdd.focusField() })
  }

  // Hand the keyboard back to the row cursor. Focus does not return on its own: a
  // TextField that gives up focus leaves the window with no active focus item at all,
  // and PanelKeyCatcher only sees keys while it holds it — so without this, one edit
  // in the detail pane left the popup deaf until it was closed and reopened.
  function reclaimKeys() {
    Qt.callLater(function () {
      if (widget.opened && !widget.detailPickerOpen && !widget.adding
          && !widget.searching && !widget.pastingToken) {
        keyCatcher.forceActiveFocus()
      }
    })
  }

  function toggleFilters() {
    filtering = !filtering
    // Leaving a filter on behind a hidden control is how a list ends up looking
    // empty for no visible reason.
    if (!filtering) tt.clearFilters()
  }

  function closeAdd() {
    adding = false
    quickAdd.clear()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  // Esc unwinds one layer at a time rather than slamming the popup shut, so a
  // stray Esc while searching never loses the reader their place.
  function dismiss() {
    if (confirmOpen) { cancelDelete(); return }
    if (adding) { closeAdd(); return }
    if (searching || tt.search !== "") { closeSearch(true); return }
    if (filtering) { toggleFilters(); return }
    if (expandedId !== "") { expandedId = ""; return }
    if (tt.view === "project" && tt.projectFilter !== "") { tt.setProjectFilter(""); return }
    widget.close()
  }

  // The delegate at `index`. NOT `children[index]`: a Repeater is itself a child of
  // the Column it fills, so positional lookup is off by one and silently scrolls to
  // the wrong row. Matching on the delegate's own `index` cannot drift.
  function itemForIndex(index) {
    for (var i = 0; i < listColumn.children.length; i++) {
      var child = listColumn.children[i]
      if (child && child.index === index) return child
    }
    return null
  }

  function scrollIntoView() {
    if (!listFlick) return
    Qt.callLater(function () {
      var item = widget.itemForIndex(widget.cursorIndex)
      if (!item || !item.height) return
      var margin = Style.space(8)
      var top = item.mapToItem(listFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, listFlick.contentHeight - listFlick.height)
      if (top < listFlick.contentY + margin) {
        listFlick.contentY = Math.max(0, top - margin)
      } else if (bottom > listFlick.contentY + listFlick.height - margin) {
        listFlick.contentY = Math.min(maxY, bottom + margin - listFlick.height)
      }
    })
  }

  // Run `method` on this widget on every monitor. An IPC target routes to exactly
  // one handler, but the bar — and therefore this widget — exists once per screen,
  // so without this a two-monitor user gets a stale badge on one of them.
  function broadcast(method) {
    var peers = (bar && typeof bar.moduleWidgets === "function")
      ? bar.moduleWidgets(moduleName) : [widget]
    for (var i = 0; i < peers.length; i++) {
      if (peers[i] && typeof peers[i][method] === "function") peers[i][method]()
    }
  }

  function refresh() { tt.refresh() }

  // --- bar slot ---------------------------------------------------------

  visible: !hideWhenEmpty || !tt.loaded || !tt.authed || tt.badgeCount > 0
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (!opened) {
      cancelDelete()
      return
    }
    cursorActive = false
    cursorIndex = 0
    expandedId = ""
    adding = false
    searching = tt.search !== ""
    if (listFlick) listFlick.contentY = 0
    tt.refresh()
    tt.loadTags()  // cheap, and a tag created elsewhere should be filterable here
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: tt
    settings: widget.settings
    // A refresh replaces `tasks`, which rebuilds every row — including the detail
    // pane the user is typing into. Hold reads while that pane owns the keyboard.
    paused: widget.detailPickerOpen
    onLoadFailed: function (message) {
      // Truth arriving late must not strand the cursor past the end of a list
      // that shrank while the request was in flight.
      Qt.callLater(widget.clampCursor)
    }
  }

  Connections {
    target: tt
    function onTasksChanged() { Qt.callLater(widget.clampCursor) }
    function onProjectsChanged() { Qt.callLater(widget.clampCursor) }
    // Changing project scope swaps the list under the cursor, so the cursor goes
    // home. Stated once here rather than beside each of the four call sites — the
    // one that forgot it (Esc out of a project) left the cursor past the end of the
    // shorter project list, where Enter and x silently did nothing.
    function onProjectFilterChanged() { widget.cursorIndex = 0 }
    // A write has finished and the cache now holds it. The bar exists once per
    // monitor, each with its own Service and poll timer, so refreshing only this
    // instance leaves every other screen counting a task the user just completed
    // until its own timer fires — up to refreshIntervalSec later.
    function onSettled() { widget.broadcast("refresh") }
  }

  IpcHandler {
    target: widget.ipcTarget

    function open(): void { widget.open() }
    function close(): void { widget.close() }
    function show(): void { widget.open() }
    function hide(): void { widget.close() }
    function toggle(): void { widget.toggle() }
    function refresh(): string { widget.broadcast("refresh"); return "ok" }
    // No broadcast here: the add is queued, not done. Fanning out now would have every
    // peer read the list from before it. `Service.settled` fans out once it has landed.
    function add(title: string): string { tt.add(title); return "ok" }
    function count(): string { return String(tt.badgeCount) }
    function view(name: string): string { widget.setView(tt.normalizeView(name)); return tt.view }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: widget.bar
    // A vertical bar has no room for a number beside the glyph.
    text: (bar && bar.vertical) ? "󰄲" : "󰄲 " + widget.badgeText
    fontSize: Style.font.caption
    horizontalMargin: 6
    active: tt.authed && Number((tt.counts || {}).overdue || 0) > 0
    activeColor: widget.urgent
    tooltipText: "TickTick — " + widget.summary
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) tt.refresh()
      else if (buttonCode === Qt.MiddleButton) { widget.open(); Qt.callLater(widget.openAdd) }
      else widget.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    bar: widget.bar
    owner: widget
    open: widget.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    // A plain Item wrapping the catcher: when the confirm dialog is up the catcher
    // is `blocked`, which makes it a transparent pass-through, and the unaccepted
    // key then bubbles up to here. That is the only way to give a modal first
    // refusal, because ConfirmDialog has no focus handling of its own — it exposes
    // handleKey(event) and expects to be driven.
    Item {
      id: keyRoot
      anchors.fill: parent

      Keys.onPressed: function (event) {
        if (widget.confirmOpen && confirmDialog.handleKey(event)) event.accepted = true
      }

      PanelKeyCatcher {
        id: keyCatcher
        anchors.fill: parent
        // Any focused text field, an open dropdown, or the modal owns the keyboard.
        blocked: widget.confirmOpen || widget.detailPickerOpen || tagPicker.popupOpen
                 || searchField.activeFocus || tokenField.activeFocus || quickAdd.editing

        onMoveRequested: function (dx, dy) {
          if (dx !== 0) { widget.cycleView(dx > 0 ? 1 : -1); return }
          if (dy === 0) return
          if (!widget.cursorActive) { widget.cursorActive = true; return }
          widget.moveCursor(dy)
        }

        // Fires only for Enter — Space never emits it. Complete lives here.
        onReturnRequested: {
          widget._returnConsumed = true
          if (!tt.authed) return
          if (widget.pickingProject) {
            var project = widget.selectedProject()
            if (project) tt.setProjectFilter(String(project.id || ""))
            return
          }
          widget.completeAt(widget.selectedTask())
        }

        // Fires for BOTH Enter and Space, and always after returnRequested, so the
        // flag is how Space keeps "expand" to itself.
        onActivateRequested: {
          if (widget._returnConsumed) { widget._returnConsumed = false; return }
          if (!tt.authed || widget.pickingProject) return
          var task = widget.selectedTask()
          if (task) widget.toggleExpanded(task.id)
        }

        onCloseRequested: widget.dismiss()
        onDeleteRequested: if (tt.authed && !widget.pickingProject) widget.requestDelete(widget.selectedTask())
        onTabRequested: function (direction) { widget.cycleView(direction < 0 ? -1 : 1) }

        // h/j/k/l and x/X are consumed as navigation and delete before they reach
        // here, so nothing below may claim them.
        onTextKey: function (text) {
          if (!tt.authed) return
          // The number keys walk `viewOptions` in order, so the tab strip and the
          // shortcuts cannot drift apart when a view is added or reordered.
          var digit = "1234567".indexOf(text)
          if (digit >= 0 && digit < widget.views.length) {
            widget.setView(widget.views[digit])
            return
          }
          switch (text) {
          case "r": case "R": tt.refresh(); break
          // Refresh is cache-first by design; this is how you say "go and look now",
          // and it drains anything queued offline on the way.
          case "s": case "S": tt.sync(); break
          case "a": case "A": widget.openAdd(); break
          case "/": widget.openSearch(); break
          case "o": case "O": tt.cycleSort(); break
          case "f": case "F": widget.toggleFilters(); break
          case "d": case "D":
            if (!widget.pickingProject) widget.requestDelete(widget.selectedTask())
            break
          case "p": case "P":
            if (!widget.pickingProject) widget.cyclePriority(widget.selectedTask())
            break
          }
        }

        Column {
          id: column
          width: parent.width
          spacing: Style.spacing.rowGap

          PanelHero {
            width: parent.width
            title: "TickTick"
            meta: widget.summary
            foreground: widget.foreground
            fontFamily: widget.fontFamily
            iconComponent: Component {
              Text {
                text: "󰄲"
                color: Number((tt.counts || {}).overdue || 0) > 0 ? widget.urgent : widget.foreground
                font.family: widget.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          // ---------------------------------------------------- signed out

          Column {
            id: signIn
            visible: !tt.authed
            width: parent.width
            spacing: Style.spacing.rowGap

            Text {
              width: parent.width
              text: "Connect your TickTick account to see your tasks here."
              color: widget.dim
              font.family: widget.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            // The browser flow leads: it is the one people expect from a "sign in"
            // button, and it needs nothing pasted. It opens a terminal of its own
            // because it is interactive — it may ask for a client id the first time,
            // and it waits on a redirect back to localhost.
            Button {
              width: parent.width
              text: "Sign in with browser"
              bordered: true
              foreground: widget.foreground
              fontFamily: widget.fontFamily
              onClicked: tt.signIn()
            }

            Text {
              visible: tt.authMessage !== ""
              width: parent.width
              text: tt.authMessage
              color: widget.urgent
              font.family: widget.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            // The fallback, and the faster path for anyone who already has a token:
            // folded away until asked for, so the panel leads with one button.
            Button {
              visible: !widget.pastingToken
              width: parent.width
              text: "Paste an API token instead"
              bordered: false
              foreground: widget.dim
              fontFamily: widget.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: {
                widget.pastingToken = true
                Qt.callLater(function () { tokenField.forceActiveFocus() })
              }
            }

            Text {
              visible: widget.pastingToken
              width: parent.width
              text: "In the TickTick web app: avatar → Settings → Account → API Token."
              color: widget.dim
              font.family: widget.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            TextField {
              id: tokenField
              visible: widget.pastingToken
              width: parent.width
              enabled: !tt.authBusy
              password: true
              placeholderText: tt.authBusy ? "Checking…" : "API token"
              foreground: widget.foreground
              font.family: widget.fontFamily
              onAccepted: {
                tt.signInWithToken(text)
                text = ""
              }
            }

            Button {
              visible: widget.pastingToken
              width: parent.width
              text: tt.authBusy ? "Connecting…" : "Connect"
              bordered: true
              foreground: widget.foreground
              fontFamily: widget.fontFamily
              onClicked: {
                tt.signInWithToken(tokenField.text)
                tokenField.text = ""
              }
            }
          }

          // ---------------------------------------------------- signed in

          // A Flow rather than ButtonGroup: ButtonGroup is a plain Row, and seven
          // smart lists do not fit across a 420px popup on one line. Same Button
          // chrome, so the chips read identically — they just wrap.
          Flow {
            id: viewTabs
            visible: tt.authed
            width: parent.width
            spacing: Style.spacing.md

            Repeater {
              model: widget.viewOptions

              delegate: Button {
                required property var modelData
                text: String(modelData.label)
                selected: tt.view === String(modelData.value)
                bordered: true
                foreground: widget.foreground
                fontFamily: widget.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: widget.setView(String(modelData.value))
              }
            }
          }

          // ---------------------------------------------------- sort & filter

          Row {
            id: filterBar
            visible: tt.authed && widget.filtering
            width: parent.width
            spacing: Style.spacing.md

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "Filter"
              color: widget.dim
              font.family: widget.fontFamily
              font.pixelSize: Style.font.caption
            }

            Button {
              anchors.verticalCenter: parent.verticalCenter
              text: tt.priorityFilter < 0
                ? "Any priority"
                : (Model.priorityLabel(tt.priorityFilter) || "No priority")
              bordered: true
              selected: tt.priorityFilter >= 0
              foreground: widget.foreground
              fontFamily: widget.fontFamily
              fontSize: Style.font.caption
              onClicked: tt.cyclePriorityFilter()
            }

            Dropdown {
              id: tagPicker
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(150)
              label: "Tag"
              showLabel: false
              options: widget.tagOptions
              value: tt.tagFilter
              foreground: widget.foreground
              fontFamily: widget.fontFamily
              onChanged: function (value) { tt.setTagFilter(value) }
            }

            Button {
              anchors.verticalCenter: parent.verticalCenter
              text: "Sort: " + tt.sortLabel()
              bordered: true
              foreground: widget.foreground
              fontFamily: widget.fontFamily
              fontSize: Style.font.caption
              onClicked: tt.cycleSort()
            }
          }

          TextField {
            id: searchField
            visible: tt.authed && (widget.searching || tt.search !== "")
            width: parent.width
            placeholderText: "Search tasks…"
            foreground: widget.foreground
            font.family: widget.fontFamily
            text: tt.search
            onTextEdited: tt.setSearch(text)
            onAccepted: widget.closeSearch(false)
            Keys.onEscapePressed: widget.closeSearch(true)
          }

          QuickAdd {
            id: quickAdd
            visible: tt.authed && widget.adding
            width: parent.width
            service: tt
            foreground: widget.foreground
            fontFamily: widget.fontFamily
            onSubmitted: function (text) {
              tt.add(text)
              widget.closeAdd()
            }
            onDismissed: widget.closeAdd()
          }

          Text {
            id: statusLine
            visible: tt.authed && (tt.actionStatus !== "" || tt.errorText !== "")
            width: parent.width
            text: tt.actionStatus !== "" ? tt.actionStatus : tt.errorText
            color: tt.actionStatus !== "" ? widget.dim : widget.urgent
            font.family: widget.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // Breadcrumb back out of a single project.
          Button {
            visible: tt.authed && tt.view === "project" && tt.projectFilter !== ""
            text: "← All lists"
            leftAlign: true
            foreground: widget.dim
            fontFamily: widget.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: tt.setProjectFilter("")
          }

          PanelSeparator {
            visible: tt.authed
            foreground: widget.foreground
          }

          Text {
            visible: tt.authed && tt.loaded && widget.rowCount === 0
            width: parent.width
            text: tt.search !== "" ? "Nothing matches “" + Model.elide(tt.search, 30) + "”"
                : (tt.priorityFilter >= 0 || tt.tagFilter !== "") ? "Nothing matches that filter"
                : tt.view === "completed" ? "Nothing finished recently"
                : "Nothing due. Enjoy it."
            color: widget.dim
            font.family: widget.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          Flickable {
            id: listFlick
            visible: tt.authed && widget.rowCount > 0
            width: parent.width
            height: visible ? Math.min(listColumn.implicitHeight, Style.space(430)) : 0
            contentWidth: width
            contentHeight: listColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: listColumn
              width: listFlick.width
              spacing: Style.spacing.xs

              Repeater {
                model: widget.rows

                delegate: Column {
                  id: entry
                  required property int index
                  required property var modelData

                  readonly property bool isProject: widget.pickingProject
                  readonly property string rowId: String(modelData && modelData.id ? modelData.id : "")
                  readonly property bool onCursor: widget.cursorActive && widget.cursorIndex === entry.index

                  width: listColumn.width
                  spacing: Style.spacing.xs

                  PanelSectionHeader {
                    visible: text !== ""
                    text: widget.sectionFor(entry.index)
                    foreground: widget.foreground
                    fontFamily: widget.fontFamily
                  }

                  // ---- a project row (Projects tab, nothing picked yet)
                  CursorSurface {
                    visible: entry.isProject
                    width: parent.width
                    implicitHeight: Style.spacing.popupRowHeight
                    hasCursor: entry.onCursor
                    foreground: widget.foreground

                    MouseArea {
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onContainsMouseChanged: if (containsMouse) widget.setCursor(entry.index, false)
                      onClicked: {
                        tt.setProjectFilter(entry.rowId)
                      }
                    }

                    Text {
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.rowPaddingX
                      anchors.right: projectCount.left
                      anchors.rightMargin: Style.spacing.md
                      anchors.verticalCenter: parent.verticalCenter
                      text: Model.squish(entry.modelData ? entry.modelData.name : "") || "Untitled"
                      // Account data, not markup: Text sniffs for HTML by default.
                      textFormat: Text.PlainText
                      color: widget.foreground
                      font.family: widget.fontFamily
                      font.pixelSize: Style.font.body
                      elide: Text.ElideRight
                    }

                    Text {
                      id: projectCount
                      anchors.right: parent.right
                      anchors.rightMargin: Style.spacing.rowPaddingX
                      anchors.verticalCenter: parent.verticalCenter
                      text: String(Model.toInt(entry.modelData ? entry.modelData.count : 0))
                      color: widget.dim
                      font.family: widget.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  // ---- a task row
                  TaskRow {
                    id: taskRow
                    visible: !entry.isProject
                    width: parent.width
                    task: entry.modelData
                    expanded: widget.expandedId === entry.rowId
                    showProjectChip: widget.showProjectChips
                    hasCursor: entry.onCursor
                    foreground: widget.foreground
                    fontFamily: widget.fontFamily
                    urgent: widget.urgent
                    onHovered: function (isHovered) { if (isHovered) widget.setCursor(entry.index, false) }
                    onCompleteRequested: widget.completeAt(entry.modelData)
                    onDeleteRequested: widget.requestDelete(entry.modelData)
                    onExpandRequested: { widget.setCursor(entry.index); widget.toggleExpanded(entry.rowId) }
                    onPriorityCycleRequested: widget.cyclePriority(entry.modelData)
                  }

                  // Built only while it is open. The pane is a whole task editor —
                  // four fields and three dropdowns — and exactly one row can be
                  // expanded at a time, so instantiating one per row would build
                  // hundreds of controls nobody is looking at.
                  Loader {
                    id: detailLoader
                    width: parent.width
                    active: !entry.isProject && widget.expandedId === entry.rowId
                    visible: active  // a Column lays out zero-height children otherwise

                    sourceComponent: TaskDetail {
                      width: detailLoader.width
                      task: entry.modelData
                      projects: tt.projects
                      expanded: true
                      foreground: widget.foreground
                      fontFamily: widget.fontFamily
                      onItemToggled: function (itemId, done) { tt.toggleItem(entry.modelData, itemId, done) }
                      onItemAdded: function (title) { tt.addItem(entry.modelData, title) }
                      onItemRemoved: function (itemId) { tt.removeItem(entry.modelData, itemId) }
                      onMoveRequested: function (projectId) { tt.moveToProject(entry.modelData, projectId) }
                      onDueRequested: function (when) { tt.setDue(entry.modelData, when) }
                      onStartRequested: function (when) { tt.setStart(entry.modelData, when) }
                      onTagsRequested: function (tags) { tt.setTags(entry.modelData, tags) }
                      onRepeatRequested: function (rule) { tt.setRepeat(entry.modelData, rule) }
                      onReminderRequested: function (spec) { tt.setReminder(entry.modelData, spec) }
                      // An open dropdown or a focused field owns j/k/Enter; tell the key
                      // catcher to stand down for as long as that is true.
                      onEditingChanged: {
                        widget.detailPickerOpen = editing
                        if (!editing) widget.reclaimKeys()
                      }
                      // Committing a change writes through Service *before* the control
                      // finishes closing, and that write replaces `tasks`, which rebuilds
                      // every delegate — so this handler is destroyed before it can report
                      // the close, and the catcher would stay deaf forever. The destructor
                      // still runs in a live context, so it is the falling edge.
                      Component.onDestruction: {
                        if (editing) {
                          widget.detailPickerOpen = false
                          widget.reclaimKeys()
                        }
                      }
                    }
                  }
                }
              }
            }
          }

          Text {
            visible: tt.authed
            width: parent.width
            text: "↑↓ move · ⏎ done · space detail · ←→ view · a add · / find · "
                  + "f filter · o sort · s sync · x delete"
            color: Qt.darker(widget.foreground, 1.8)
            font.family: widget.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
        }
      }

      ConfirmDialog {
        id: confirmDialog
        anchors.fill: parent
        z: 10
        opened: widget.confirmOpen
        message: widget.confirmTask
          ? "Delete “" + Model.elide(widget.confirmTask.title, 42) + "”?\nThis cannot be undone."
          : ""
        confirmText: "Delete"
        cancelText: "Keep"
        foreground: widget.foreground
        fontFamily: widget.fontFamily
        onCanceled: widget.cancelDelete()
        onConfirmed: widget.confirmDeleteNow()
      }
    }
  }
}
