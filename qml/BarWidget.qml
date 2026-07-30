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

  readonly property bool hideWhenEmpty: tt.boolSetting("hideWhenEmpty", false)
  readonly property bool showProjectChips: tt.boolSetting("showProjectChips", true)
  readonly property bool confirmDelete: tt.boolSetting("confirmDelete", true)

  // --- cursor / mode state ----------------------------------------------

  property bool cursorActive: false
  property int cursorIndex: 0
  property string expandedId: ""
  property bool searching: false
  property bool adding: false
  property bool detailPickerOpen: false
  property bool confirmOpen: false
  property var confirmTask: null
  // Enter emits returnRequested and THEN activateRequested. Space emits only
  // activateRequested. This flag is how the two are told apart.
  property bool _returnConsumed: false

  readonly property var views: ["today", "next", "all", "project"]
  readonly property var viewOptions: [
    { value: "today", label: "Today" },
    { value: "next", label: "Next" },
    { value: "all", label: "All" },
    { value: "project", label: "Projects" }
  ]

  // The Projects tab shows the project list until one is picked; after that it is
  // an ordinary task list scoped to that project.
  readonly property bool pickingProject: tt.view === "project" && tt.projectFilter === ""
  readonly property var rows: pickingProject ? (tt.projects || []) : (tt.tasks || [])
  readonly property int rowCount: rows.length

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

  function setCursor(index) {
    cursorActive = true
    cursorIndex = Math.max(0, Math.min(rowCount - 1, index))
    scrollIntoView()
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

  // The section a row opens, or "" when the row above is in the same bucket.
  function sectionFor(index) {
    if (pickingProject) return index === 0 ? "PROJECTS" : ""
    var list = rows
    if (index < 0 || index >= list.length) return ""
    var bucket = String(list[index].bucket || "")
    if (index > 0 && String(list[index - 1].bucket || "") === bucket) return ""
    return Model.sectionTitle(bucket).toUpperCase()
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
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: tt
    settings: widget.settings
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
  }

  IpcHandler {
    target: widget.ipcTarget

    function open(): void { widget.open() }
    function close(): void { widget.close() }
    function show(): void { widget.open() }
    function hide(): void { widget.close() }
    function toggle(): void { widget.toggle() }
    function refresh(): string { widget.broadcast("refresh"); return "ok" }
    function add(title: string): string { tt.add(title); widget.broadcast("refresh"); return "ok" }
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
        blocked: widget.confirmOpen || widget.detailPickerOpen
                 || searchField.activeFocus || quickAdd.editing

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
            if (project) { tt.setProjectFilter(String(project.id || "")); widget.cursorIndex = 0 }
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
          switch (text) {
          case "r": case "R": tt.refresh(); break
          case "a": case "A": widget.openAdd(); break
          case "/": widget.openSearch(); break
          case "d": case "D":
            if (!widget.pickingProject) widget.requestDelete(widget.selectedTask())
            break
          case "p": case "P":
            if (!widget.pickingProject) widget.cyclePriority(widget.selectedTask())
            break
          case "1": widget.setView("today"); break
          case "2": widget.setView("next"); break
          case "3": widget.setView("all"); break
          case "4": widget.setView("project"); break
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
              text: "Paste a TickTick API token to connect. In the TickTick web app: "
                    + "avatar → Settings → Account → API Token."
              color: widget.dim
              font.family: widget.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            TextField {
              id: tokenField
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

            Text {
              visible: tt.authMessage !== ""
              width: parent.width
              text: tt.authMessage
              color: widget.urgent
              font.family: widget.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Button {
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

            Button {
              width: parent.width
              text: "Use browser sign-in instead"
              bordered: false
              foreground: widget.dim
              fontFamily: widget.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: tt.signIn()
            }
          }

          // ---------------------------------------------------- signed in

          ButtonGroup {
            id: viewTabs
            visible: tt.authed
            options: widget.viewOptions
            value: tt.view
            foreground: widget.foreground
            fontFamily: widget.fontFamily
            // ButtonGroup is stateless by design: it reports the choice and leaves
            // the assignment to us.
            onChanged: function (value) { widget.setView(value) }
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
            text: "← All projects"
            leftAlign: true
            foreground: widget.dim
            fontFamily: widget.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: { tt.setProjectFilter(""); widget.cursorIndex = 0 }
          }

          PanelSeparator {
            visible: tt.authed
            foreground: widget.foreground
          }

          Text {
            visible: tt.authed && tt.loaded && widget.rowCount === 0
            width: parent.width
            text: tt.search !== "" ? "Nothing matches “" + Model.elide(tt.search, 30) + "”"
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
                      onContainsMouseChanged: if (containsMouse) widget.setCursor(entry.index)
                      onClicked: {
                        tt.setProjectFilter(entry.rowId)
                        widget.cursorIndex = 0
                      }
                    }

                    Text {
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.rowPaddingX
                      anchors.right: projectCount.left
                      anchors.rightMargin: Style.spacing.md
                      anchors.verticalCenter: parent.verticalCenter
                      text: Model.squish(entry.modelData ? entry.modelData.name : "") || "Untitled"
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
                    onHovered: function (isHovered) { if (isHovered) widget.setCursor(entry.index) }
                    onCompleteRequested: widget.completeAt(entry.modelData)
                    onDeleteRequested: widget.requestDelete(entry.modelData)
                    onExpandRequested: { widget.setCursor(entry.index); widget.toggleExpanded(entry.rowId) }
                    onPriorityCycleRequested: widget.cyclePriority(entry.modelData)
                  }

                  TaskDetail {
                    id: taskDetail
                    visible: !entry.isProject && expanded && hasDetail
                    width: parent.width
                    task: entry.modelData
                    projects: tt.projects
                    expanded: widget.expandedId === entry.rowId
                    foreground: widget.foreground
                    fontFamily: widget.fontFamily
                    onItemToggled: function (itemId, done) { tt.toggleItem(entry.modelData, itemId, done) }
                    onMoveRequested: function (projectId) { tt.moveToProject(entry.modelData, projectId) }
                    // An open dropdown owns j/k/Enter; tell the key catcher to stand down.
                    onPickerOpenChanged: widget.detailPickerOpen = pickerOpen
                  }
                }
              }
            }
          }

          Text {
            visible: tt.authed
            width: parent.width
            text: "↑↓ move · ⏎ complete · space detail · ←→ view · a add · / search · x delete"
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
