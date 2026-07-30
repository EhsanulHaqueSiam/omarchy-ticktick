import QtQuick
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Data layer for the TickTick widget: every call shells out to `bin/ticktick`,
// which prints exactly one JSON object. Reads and writes get their own Process
// so a slow poll never blocks a click, and writes are serialized through a queue
// so two fast clicks cannot race one process slot.
Item {
  id: root

  // Injected by BarWidget.qml from this widget's shell.json entry.
  property var settings: ({})

  property var tasks: []
  property var projects: []
  property var counts: root.emptyCounts()
  property bool authed: true
  property bool loaded: false
  property bool refreshing: false
  property string errorText: ""
  property string actionStatus: ""
  property string view: root.normalizeView(root.setting("defaultView", "today"))
  property string projectFilter: ""
  property string search: ""

  signal loadFailed(string message)

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 300, 30, 86400)
  readonly property int upcomingDays: intSetting("upcomingDays", 7, 0, 30)
  readonly property string helperPath: String(Qt.resolvedUrl("../bin/ticktick")).replace(/^file:\/\//, "")

  readonly property int badgeCount: {
    var mode = String(setting("badgeMode", "Today")).toLowerCase()
    var c = counts || {}
    if (mode === "none") return 0
    if (mode === "overdue") return Math.max(0, Number(c.overdue || 0))
    if (mode === "all") return Math.max(0, Number(c.total || 0))
    return Math.max(0, Number(c.overdue || 0) + Number(c.today || 0))
  }

  property bool authBusy: false
  property string authMessage: ""

  property var _queue: []
  property bool _refreshQueued: false
  property string _preview: ""
  property string _pendingToken: ""

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    if (n < min) n = min
    if (n > max) n = max
    return n
  }

  // `omarchy bar plugin set <id> <key> <value>` writes a STRING unless the caller
  // remembers `--json`, so a boolean setting arrives as "true" far more often than
  // as true. Comparing that with `=== true` silently ignores everything the user
  // typed, which is the worst kind of wrong: the setting appears to save and then
  // does nothing.
  function boolSetting(name, fallback) {
    var value = setting(name, fallback)
    if (typeof value === "boolean") return value
    var text = String(value).trim().toLowerCase()
    if (text === "true" || text === "1" || text === "yes" || text === "on") return true
    if (text === "false" || text === "0" || text === "no" || text === "off") return false
    return fallback === true
  }

  function emptyCounts() {
    return { overdue: 0, today: 0, tomorrow: 0, upcoming: 0, later: 0, undated: 0, total: 0 }
  }

  // The settings enum and the UI both spell views for humans; views.py wants
  // its own five names.
  function normalizeView(name) {
    var value = String(name || "").toLowerCase().replace(/[^a-z]/g, "")
    if (value === "next7" || value === "next" || value === "upcoming") return "next"
    if (value === "projects" || value === "project") return "project"
    if (value === "overdue") return "overdue"
    if (value === "all") return "all"
    return "today"
  }

  // --- commands ---------------------------------------------------------

  function refresh() {
    if (readProcess.running) {
      _refreshQueued = true
      return
    }
    // views.collect rejects a project view with no id; fall back rather than
    // trading the whole list for a usage error.
    var effective = view === "project" && projectFilter === "" ? "all" : view
    var args = ["tasks", "--view", effective, "--days", String(upcomingDays)]
    if (effective === "project") args = args.concat(["--project", projectFilter])
    if (search !== "") args = args.concat(["--search", search])
    if (boolSetting("includeUndated", false)) args.push("--include-undated")
    refreshing = true
    readProcess.command = ["python3", helperPath].concat(args)
    readProcess.running = true
  }

  function setView(name) {
    view = normalizeView(name)
  }

  function setProjectFilter(projectId) {
    projectFilter = String(projectId || "")
  }

  function setSearch(text) {
    search = Model.squish(text)
  }

  function complete(task) {
    var row = taskRow(task)
    if (!row) return
    dropTask(row.id)
    toast("Completed " + Model.elide(row.title, 40))
    enqueue(["complete", row.id].concat(scopeArgs(row)))
  }

  function remove(task) {
    var row = taskRow(task)
    if (!row) return
    dropTask(row.id)
    toast("Deleted " + Model.elide(row.title, 40))
    enqueue(["delete", row.id].concat(scopeArgs(row), ["--yes"]))
  }

  function add(text) {
    var value = Model.squish(text)
    if (value === "") return
    var args = ["add", value]
    var target = String(setting("defaultProject", ""))
    if (target !== "") args = args.concat(["--project", target])
    toast("Adding…")
    enqueue(args)
  }

  function addDetailed(fields) {
    var f = fields || {}
    var title = Model.squish(f.title)
    if (title === "") return
    var args = ["add", title]
    var target = String(f.project || setting("defaultProject", ""))
    if (target !== "") args = args.concat(["--project", target])
    if (f.due) args = args.concat(["--due", String(f.due)])
    if (f.priority !== undefined && f.priority !== null && f.priority !== "") args = args.concat(["--priority", String(f.priority)])
    if (f.content) args = args.concat(["--content", String(f.content)])
    if (f.repeat) args = args.concat(["--repeat", String(f.repeat)])
    toast("Adding…")
    enqueue(args)
  }

  function setPriority(task, priority) {
    var row = taskRow(task)
    if (!row) return
    var value = Model.toInt(priority)
    patchTask(row.id, { priority: value })
    toast(Model.priorityLabel(value) || "No priority")
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--priority", String(value)]))
  }

  function moveToProject(task, projectId) {
    var row = taskRow(task)
    if (!row) return
    var target = String(projectId || "")
    if (target === "" || target === row.projectId) return
    var meta = projectById(target)
    patchTask(row.id, { projectId: target, project: meta.name, projectColor: meta.color })
    toast("Moved to " + (meta.name || target))
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--move-to", target]))
  }

  function toggleItem(task, itemId, done) {
    var row = taskRow(task)
    var id = String(itemId || "")
    if (!row || id === "") return
    var wanted = done === true
    var items = []
    var ticked = 0
    for (var i = 0; i < (row.items || []).length; i++) {
      var item = row.items[i]
      var next = { id: item.id, title: item.title, done: item.id === id ? wanted : item.done === true }
      if (next.done) ticked += 1
      items.push(next)
    }
    patchTask(row.id, { items: items, itemsDone: ticked })
    var args = ["check", row.id, id].concat(scopeArgs(row))
    if (!wanted) args.push("--uncheck")
    enqueue(args)
  }

  // Sign in by handing the helper a TickTick API token.
  //
  // The token goes over the process's STDIN, never in `command`. Anything in argv
  // is readable by every user on the machine through /proc/<pid>/cmdline for as
  // long as the process lives, and a task API token is a full account credential.
  function signInWithToken(token) {
    var value = Model.squish(token)
    if (value === "" || loginProcess.running) return
    _pendingToken = value
    authBusy = true
    authMessage = ""
    loginProcess.command = ["python3", helperPath, "login"]
    loginProcess.running = true
  }

  function signOut() {
    enqueue(["logout"])
    authed = false
    authMessage = ""
  }

  function signIn() {
    // The browser OAuth flow is interactive, so it needs a terminal of its own.
    // Kept as the second path for anyone who would rather not paste a token.
    var command = "python3 " + Util.shellQuote(helperPath) + " auth"
    Util.execDetached("omarchy-launch-floating-terminal-with-presentation " + Util.shellQuote(command))
    toast("Finish signing in, then come back")
    authRamp.ticks = 0
    authRamp.running = true
  }

  function parsePreview(text) {
    var next = Model.squish(text)
    if (next === "") return ""
    // Reading _preview inside this function is what makes it work: a binding on
    // parsePreview(field.text) picks up the dependency and re-evaluates when the
    // helper answers, which a synchronous return value otherwise cannot do.
    if (next !== parseTimer.pending) {
      parseTimer.pending = next
      parseTimer.restart()
    }
    return _preview
  }

  // --- internals --------------------------------------------------------

  function taskRow(task) {
    if (!task) return null
    if (typeof task === "string") {
      for (var i = 0; i < tasks.length; i++) if (tasks[i].id === task) return tasks[i]
      return null
    }
    return task.id ? task : null
  }

  function scopeArgs(row) {
    return row && row.projectId ? ["--project", String(row.projectId)] : []
  }

  function projectById(projectId) {
    for (var i = 0; i < projects.length; i++) if (projects[i].id === projectId) return projects[i]
    return { id: projectId, name: "", color: "" }
  }

  // Optimistic removal: the row leaves the list and the badge on the click, and
  // the refresh that follows the queue draining puts it back if the write failed.
  function dropTask(taskId) {
    var kept = []
    var gone = null
    for (var i = 0; i < tasks.length; i++) {
      if (tasks[i].id === taskId) gone = tasks[i]
      else kept.push(tasks[i])
    }
    if (!gone) return
    tasks = kept
    var next = {}
    for (var key in counts) next[key] = counts[key]
    if (next[gone.bucket] > 0) next[gone.bucket] -= 1
    if (next.total > 0) next.total -= 1
    counts = next
  }

  function patchTask(taskId, changes) {
    var out = []
    for (var i = 0; i < tasks.length; i++) {
      var row = tasks[i]
      if (row.id !== taskId) {
        out.push(row)
        continue
      }
      var copy = {}
      for (var key in row) copy[key] = row[key]
      for (var change in changes) copy[change] = changes[change]
      out.push(copy)
    }
    tasks = out
  }

  function enqueue(args) {
    _queue.push(args)
    pump()
  }

  function pump() {
    if (actionProcess.running || _queue.length === 0) return
    actionProcess.command = ["python3", helperPath].concat(_queue.shift())
    actionProcess.running = true
  }

  function toast(text) {
    actionStatus = Model.elide(text, 80)
    toastTimer.restart()
  }

  function payloadOf(raw) {
    var text = String(raw || "").trim()
    if (text === "") return {}
    try {
      return JSON.parse(text)
    } catch (e) {
      return Util.parseModuleJson(text)
    }
  }

  // cli.py may print the view payload flat or under `data`; accept both rather
  // than blanking the widget over a key name.
  function bodyOf(payload) {
    return Util.isPlainObject(payload) && Util.isPlainObject(payload.data) ? payload.data : (payload || {})
  }

  function messageOf(payload, stderr, fallback) {
    return Model.squish((payload && (payload.message || payload.text)) || stderr || fallback)
  }

  function applyRead(stdout, stderr) {
    var payload = payloadOf(stdout)
    if (payload && payload.ok === true) {
      var body = bodyOf(payload)
      // Stale-while-revalidating: only a good payload is allowed to replace what
      // is on screen, so a failed poll never blanks the list.
      if (Array.isArray(body.tasks)) tasks = body.tasks
      if (Array.isArray(body.projects)) projects = body.projects
      if (Util.isPlainObject(body.counts)) counts = body.counts
      authed = true
      errorText = ""
      return
    }
    var message = messageOf(payload, stderr, "Could not read TickTick tasks")
    if (payload && payload.error === "auth") {
      authed = false
      errorText = ""
    } else {
      errorText = Model.elide(message, 140)
    }
    loadFailed(message)
  }

  function applyAction(stdout, stderr) {
    var payload = payloadOf(stdout)
    if (payload && payload.ok === true) {
      authed = true
      errorText = ""
      return
    }
    var message = messageOf(payload, stderr, "TickTick command failed")
    if (payload && payload.error === "auth") {
      authed = false
      errorText = ""
    } else {
      errorText = Model.elide(message, 140)
    }
    toast(message)
  }

  onViewChanged: Qt.callLater(root.refresh)
  onProjectFilterChanged: Qt.callLater(root.refresh)
  onSearchChanged: searchTimer.restart()

  Timer {
    // Runs whether or not the popup is open: the bar badge is the whole point of
    // the widget, and it has to be right before anyone clicks.
    id: pollTimer
    // While signed out there is nothing to poll for except the sign-in itself, and
    // that can happen in a terminal — `ticktick login`, or another TickTick tool
    // whose token this one adopts. At the normal cadence the widget would still be
    // asking you to sign in minutes after you had. Reads are cache-first, so a short
    // interval here costs a process spawn, not a request.
    interval: (root.authed ? root.refreshIntervalSec : 15) * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // Search is server-side (counts stay whole-account), so typing must not
    // spawn a helper per keystroke.
    id: searchTimer
    interval: 300
    onTriggered: root.refresh()
  }

  Timer {
    id: toastTimer
    interval: 4000
    onTriggered: root.actionStatus = ""
  }

  Timer {
    id: parseTimer
    property string pending: ""
    interval: 250
    onTriggered: {
      if (parseProcess.running) {
        restart()
        return
      }
      if (pending === "") return
      parseProcess.command = ["python3", root.helperPath, "parse", pending]
      parseProcess.running = true
    }
  }

  Timer {
    // The poll interval defaults to five minutes; without a ramp the widget
    // would still be asking you to sign in long after you did.
    id: authRamp
    property int ticks: 0
    interval: 5000
    repeat: true
    running: false
    onTriggered: {
      ticks += 1
      if (root.authed || ticks >= 24) {
        ticks = 0
        running = false
      } else {
        root.refresh()
      }
    }
  }

  Process {
    id: readProcess
    running: false
    command: []
    stdout: StdioCollector { id: readOut; waitForEnd: true }
    stderr: StdioCollector { id: readErr; waitForEnd: true }
    onExited: function () {
      root.refreshing = false
      root.loaded = true
      root.applyRead(String(readOut.text || ""), String(readErr.text || ""))
      if (root._refreshQueued) {
        root._refreshQueued = false
        Qt.callLater(root.refresh)
      }
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    stderr: StdioCollector { id: actionErr; waitForEnd: true }
    onExited: function () {
      root.applyAction(String(actionOut.text || ""), String(actionErr.text || ""))
      // Deferred so the process slot is free again; one refresh when the queue
      // drains reconciles every optimistic edit at once.
      if (root._queue.length > 0) Qt.callLater(root.pump)
      else Qt.callLater(root.refresh)
    }
  }

  Process {
    // Sign-in gets its own slot so a paste is never queued behind a poll, and so
    // stdin can be enabled here without every other call inheriting it.
    id: loginProcess
    running: false
    command: []
    stdinEnabled: true
    stdout: StdioCollector { id: loginOut; waitForEnd: true }
    stderr: StdioCollector { id: loginErr; waitForEnd: true }

    onStarted: {
      loginProcess.write(root._pendingToken + "\n")
      root._pendingToken = ""
      // Closing stdin is what lets the helper's read() return instead of blocking
      // until the timeout; without it sign-in appears to hang.
      loginProcess.stdinEnabled = false
    }

    onExited: function () {
      root.authBusy = false
      root._pendingToken = ""
      var payload = root.payloadOf(String(loginOut.text || ""))
      if (payload && payload.ok === true) {
        root.authed = true
        root.authMessage = ""
        root.errorText = ""
        root.toast("Signed in to TickTick")
        Qt.callLater(root.refresh)
        return
      }
      root.authMessage = Model.elide(
        root.messageOf(payload, String(loginErr.text || ""), "Could not sign in"), 200)
    }
  }

  Process {
    // Its own slot: the preview must never wait behind a write, nor cancel one.
    id: parseProcess
    running: false
    command: []
    stdout: StdioCollector { id: parseOut; waitForEnd: true }
    onExited: function () {
      var payload = root.payloadOf(String(parseOut.text || ""))
      root._preview = payload && payload.ok === true ? Model.parseSummary(root.bodyOf(payload)) : ""
    }
  }
}
