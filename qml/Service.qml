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
  property var groups: []
  property var tags: []
  property var counts: root.emptyCounts()
  property bool authed: true
  property bool loaded: false
  property bool refreshing: false
  property string errorText: ""
  property string actionStatus: ""
  property string view: root.normalizeView(root.setting("defaultView", "today"))
  property string projectFilter: ""
  property string search: ""
  // How rows are ordered, and therefore what the section headings say. "list" is
  // TickTick's own default for a smart list: group under the list each task is in.
  property string sort: root.normalizeSort(root.setting("sortBy", "list"))
  // -1 means "any"; 0 is a real TickTick priority ("none"), so it cannot be the
  // sentinel. Same for the tag: "" is any, and no tag is named "".
  property int priorityFilter: -1
  property string tagFilter: ""

  signal loadFailed(string message)
  // A queued write has drained and the cache is authoritative again. Whoever owns
  // the widget decides what to do about it; this layer knows nothing about monitors.
  signal settled()

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 300, 30, 86400)
  readonly property int upcomingDays: intSetting("upcomingDays", 7, 0, 30)
  readonly property int completedDays: intSetting("completedDays", 7, 1, 90)
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
  // Set by whoever owns the UI while something on screen must not be pulled out from
  // under the user — a detail pane being edited. Reads queue up behind it.
  property bool paused: false

  property var _queue: []
  property bool _refreshQueued: false
  property string _preview: ""
  property string _previewTitle: ""
  property string _previewFor: ""
  property int _draftSeq: 0
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
    return {
      overdue: 0, today: 0, tomorrow: 0, upcoming: 0, later: 0, undated: 0,
      notes: 0, total: 0
    }
  }

  // The settings enum and the UI both spell views for humans; views.py wants
  // its own names. `completed` is not one of them — it is a different command
  // and a different endpoint — but it is a tab like any other from here.
  function normalizeView(name) {
    var value = String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "")
    if (value === "next7" || value === "next" || value === "upcoming"
        || value === "next7days") return "next"
    if (value === "projects" || value === "project" || value === "lists"
        || value === "list") return "project"
    if (value === "tomorrow") return "tomorrow"
    if (value === "inbox") return "inbox"
    if (value === "overdue") return "overdue"
    if (value === "completed" || value === "done") return "completed"
    if (value === "all") return "all"
    return "today"
  }

  function normalizeSort(name) {
    var value = String(name || "").toLowerCase().replace(/[^a-z]/g, "")
    if (value === "time" || value === "date" || value === "due") return "time"
    if (value === "priority") return "priority"
    if (value === "title" || value === "name") return "title"
    return "list"
  }

  // --- commands ---------------------------------------------------------

  function refresh() {
    // Replacing `tasks` rebuilds every delegate in the list, which destroys whatever
    // field is being typed into and the text in it. A poll landing mid-edit must not
    // do that, so it waits — deferred, never dropped.
    if (paused || readProcess.running) {
      _refreshQueued = true
      return
    }
    refreshing = true
    readProcess.command = ["python3", helperPath].concat(readArgs())
    readProcess.running = true
  }

  function readArgs() {
    // Finished work lives behind a different endpoint — /task/filter and
    // /project/{id}/data both return undone tasks only — so it is a different
    // command. The rows come back in the same shape, which is why the same
    // delegate paints them.
    if (view === "completed") {
      var done = ["completed", "--days", String(completedDays)]
      if (projectFilter !== "") done = done.concat(["--project", projectFilter])
      return done
    }
    // views.collect rejects a project view with no id; fall back rather than
    // trading the whole list for a usage error.
    var effective = view === "project" && projectFilter === "" ? "all" : view
    var args = ["tasks", "--view", effective, "--days", String(upcomingDays),
                "--sort", sort]
    if (effective === "project") args = args.concat(["--project", projectFilter])
    if (search !== "") args = args.concat(["--search", search])
    if (priorityFilter >= 0) args = args.concat(["--priority", String(priorityFilter)])
    if (tagFilter !== "") args = args.concat(["--tag", tagFilter])
    if (boolSetting("includeUndated", false)) args.push("--include-undated")
    return args
  }

  function setView(name) {
    view = normalizeView(name)
  }

  function setSort(name) {
    sort = normalizeSort(name)
  }

  function cycleSort() {
    var order = ["list", "time", "priority", "title"]
    var at = order.indexOf(sort)
    setSort(order[(at < 0 ? 0 : at + 1) % order.length])
    toast("Sorted by " + sortLabel())
  }

  // What the UI should section on. The Done view comes from a different command with
  // no `sort` at all — its rows are ordered by when they were finished — so heading
  // them with list names would cut across an order nothing else produced.
  readonly property string effectiveSort: view === "completed" ? "time" : sort

  function sortLabel() {
    if (sort === "time") return "date"
    if (sort === "priority") return "priority"
    if (sort === "title") return "title"
    return "list"
  }

  function setPriorityFilter(priority) {
    priorityFilter = Model.toInt(priority) >= 0 ? Model.toInt(priority) : -1
  }

  function cyclePriorityFilter() {
    // -1 (any) then TickTick's four real levels, so the wrap always returns to "any".
    var order = [-1, 5, 3, 1, 0]
    var at = order.indexOf(priorityFilter)
    priorityFilter = order[(at < 0 ? 0 : at + 1) % order.length]
    toast(priorityFilter < 0 ? "Any priority"
                             : (Model.priorityLabel(priorityFilter) || "No priority") + " only")
  }

  function setTagFilter(tag) {
    tagFilter = Model.squish(tag).replace(/^#+/, "")
  }

  function clearFilters() {
    priorityFilter = -1
    tagFilter = ""
    setSearch("")
  }

  // Force a full round trip: drain the outbox, then refetch however fresh the
  // cache thinks it is. `refresh` is cache-first by design, so without this there
  // is no way to say "go and look now".
  function sync() {
    toast("Syncing…")
    enqueue(["sync"])
  }

  function setProjectFilter(projectId) {
    projectFilter = String(projectId || "")
  }

  function setSearch(text) {
    search = Model.squish(text)
  }

  function complete(task) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    if (Model.isNote(row)) {
      // TickTick has no way to finish a note, and the API would reject it.
      toast("Notes cannot be completed")
      return
    }
    dropTask(row.id)
    toast("Completed " + Model.elide(row.title, 40))
    enqueue(["complete", row.id].concat(scopeArgs(row)))
  }

  function remove(task) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    dropTask(row.id)
    toast("Deleted " + Model.elide(row.title, 40))
    enqueue(["delete", row.id].concat(scopeArgs(row), ["--yes"]))
  }

  // Where a task typed *here* belongs, the way TickTick decides it: inside a list it
  // joins that list, and inside a dated smart list it takes that date — otherwise
  // every task added from Today would land undated, somewhere the user is not looking.
  function addTarget() {
    if (view === "project" && projectFilter !== "") return projectFilter
    if (view === "inbox") return ""  // the Inbox is where a task with no list goes
    return String(setting("defaultProject", ""))
  }

  function addDueDefault() {
    if (view === "today" || view === "overdue" || view === "next") return "today"
    if (view === "tomorrow") return "tomorrow"
    return ""
  }

  function add(text) {
    var value = Model.squish(text)
    if (value === "") return
    var target = addTarget()
    var when = addDueDefault()
    var args = ["add", value]
    if (target !== "") args = args.concat(["--project", target])
    if (when !== "") args = args.concat(["--due-default", when])
    // On screen before the helper has even been spawned. Creating a task is two
    // process launches and a round trip away from being confirmed, and staring at an
    // unchanged list for that long reads as "it didn't work".
    showDraft(value, target, when)
    enqueue(args)
  }

  // A stand-in row for a task that exists locally and nowhere else yet. Every key the
  // row shape promises is present: a delegate binding to a missing one paints the
  // literal string "undefined". The refresh that follows the write replaces it with
  // the server's copy, id, parsed date and all.
  function showDraft(text, projectId, when) {
    if (view === "completed") return  // nothing typed now is something already done
    var meta = projectById(projectId)
    var bucket = when === "today" ? "today" : (when === "tomorrow" ? "tomorrow" : "undated")
    var draft = {
      id: "local-draft-" + (_draftSeq++), projectId: projectId,
      project: meta.name, projectColor: meta.color,
      title: draftTitle(text), content: "",
      bucket: bucket, due: 0, dueLabel: when, dueIso: "",
      priority: 0, isAllDay: false, repeat: "", reminders: [],
      items: [], itemsDone: 0, itemsTotal: 0, tags: [],
      start: 0, startLabel: "", startIso: "", parentId: "", sortOrder: 0,
      status: 0, progress: 0, pending: true, kind: "TEXT", isNote: false
    }
    insertDraft(draft)
    var next = {}
    for (var key in counts) next[key] = counts[key]
    next[bucket] = Model.toInt(next[bucket]) + 1
    next.total = Model.toInt(next.total) + 1
    counts = next
  }

  // Into its own group, not onto the end. The UI opens a section wherever the section
  // key changes, so a draft appended after the last row would open a *second* heading
  // for a list that is already on screen further up.
  function insertDraft(draft) {
    var key = Model.sectionKey(draft, effectiveSort)
    var out = []
    var placed = false
    for (var i = 0; i < tasks.length; i++) {
      out.push(tasks[i])
      if (placed || key === "") continue
      var last = i + 1 >= tasks.length || Model.sectionKey(tasks[i + 1], effectiveSort) !== key
      if (Model.sectionKey(tasks[i], effectiveSort) === key && last) {
        out.push(draft)
        placed = true
      }
    }
    if (!placed) out.push(draft)
    tasks = out
  }

  // The title with its metadata stripped, when the quick-add preview happens to have
  // resolved this exact text already — it usually has, because the preview runs while
  // the user is still typing. Otherwise the raw line, which is at worst briefly untidy.
  function draftTitle(text) {
    return _previewFor === text && _previewTitle !== "" ? _previewTitle : text
  }

  function addDetailed(fields) {
    var f = fields || {}
    var title = Model.squish(f.title)
    if (title === "") return
    var args = ["add", title]
    var target = String(f.project || addTarget())
    if (target !== "") args = args.concat(["--project", target])
    if (f.due) args = args.concat(["--due", String(f.due)])
    else if (addDueDefault() !== "") args = args.concat(["--due-default", addDueDefault()])
    if (f.priority !== undefined && f.priority !== null && f.priority !== "") args = args.concat(["--priority", String(f.priority)])
    if (f.content) args = args.concat(["--content", String(f.content)])
    if (f.repeat) args = args.concat(["--repeat", String(f.repeat)])
    showDraft(title, target, f.due ? "" : addDueDefault())
    enqueue(args)
  }

  function setPriority(task, priority) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var value = Model.toInt(priority)
    patchTask(row.id, { priority: value })
    toast(Model.priorityLabel(value) || "No priority")
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--priority", String(value)]))
  }

  function moveToProject(task, projectId) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var target = String(projectId || "")
    if (target === "" || target === row.projectId) return
    var meta = projectById(target)
    patchTask(row.id, { projectId: target, project: meta.name, projectColor: meta.color })
    toast("Moved to " + (meta.name || target))
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--move-to", target]))
  }

  // --- editing one task -------------------------------------------------
  //
  // Each of these is optimistic in the same two steps: patch the cached row so the
  // UI moves now, then queue the write. `edit` echoes the server's row and the
  // refresh that follows the queue draining reconciles anything we guessed wrong.

  function setDue(task, when) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(when)
    var args = ["edit", row.id].concat(scopeArgs(row))
    // Optimistic on the *label* only: what "next friday" resolves to is the
    // helper's business, and guessing it here would print two different answers.
    if (text === "") {
      patchTask(row.id, { due: 0, dueIso: "", dueLabel: "", bucket: "undated" })
      toast("Due date cleared")
      args.push("--clear-due")
    } else {
      patchTask(row.id, { dueLabel: text, pending: true })
      toast("Due " + Model.elide(text, 40))
      args = args.concat(["--due", text])
    }
    enqueue(args)
  }

  function setStart(task, when) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(when)
    if (text === "") return
    patchTask(row.id, { startLabel: text, pending: true })
    toast("Starts " + Model.elide(text, 40))
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--start", text]))
  }

  function setTags(task, tags) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var list = Model.tagList(tags)
    patchTask(row.id, { tags: list })
    toast(list.length === 0 ? "Tags cleared" : Model.tagText(list))
    var args = ["edit", row.id].concat(scopeArgs(row))
    // No --tag at all means "replace with nothing", which is how the helper
    // spells clearing them; one --tag per name otherwise.
    if (list.length === 0) args = args.concat(["--tag", ""])
    for (var i = 0; i < list.length; i++) args = args.concat(["--tag", list[i]])
    enqueue(args)
  }

  function setRepeat(task, rule) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(rule)
    var args = ["edit", row.id].concat(scopeArgs(row))
    patchTask(row.id, { repeat: text === "" ? "" : String(row.repeat || "") , pending: true })
    if (text === "") {
      toast("No longer repeats")
      args.push("--clear-repeat")
    } else {
      toast("Repeats " + text)
      args = args.concat(["--repeat", text])
    }
    enqueue(args)
  }

  function setReminder(task, spec) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(spec)
    var args = ["edit", row.id].concat(scopeArgs(row))
    if (text === "") {
      patchTask(row.id, { reminders: [] })
      toast("Reminder cleared")
      args.push("--clear-remind")
    } else {
      toast("Reminder " + text)
      args = args.concat(["--remind", text])
    }
    enqueue(args)
  }

  function setTitle(task, title) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(title)
    if (text === "" || text === Model.squish(row.title)) return
    patchTask(row.id, { title: text })
    toast("Renamed")
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--title", text]))
  }

  function setContent(task, content) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = String(content === undefined || content === null ? "" : content)
    patchTask(row.id, { content: text })
    toast("Note saved")
    enqueue(["edit", row.id].concat(scopeArgs(row), ["--content", text]))
  }

  function addItem(task, title) {
    var row = taskRow(task)
    if (!row || refuseDraft(row)) return
    var text = Model.squish(title)
    if (text === "") return
    // A local id so the row can paint the new item straight away; the helper mints
    // the real one and the reconciling refresh swaps it in.
    var items = (row.items || []).slice()
    items.push({ id: "local-pending-" + items.length, title: text, done: false })
    patchTask(row.id, { items: items, itemsTotal: items.length })
    toast("Added " + Model.elide(text, 40))
    enqueue(["item", "add", row.id, text].concat(scopeArgs(row)))
  }

  function removeItem(task, itemId) {
    var row = taskRow(task)
    var id = String(itemId || "")
    if (!row || id === "" || refuseDraft(row)) return
    var items = []
    var ticked = 0
    for (var i = 0; i < (row.items || []).length; i++) {
      if (row.items[i].id === id) continue
      items.push(row.items[i])
      if (row.items[i].done) ticked += 1
    }
    patchTask(row.id, { items: items, itemsDone: ticked, itemsTotal: items.length })
    enqueue(["item", "remove", row.id, id].concat(scopeArgs(row)))
  }

  function toggleItem(task, itemId, done) {
    var row = taskRow(task)
    var id = String(itemId || "")
    if (!row || id === "" || refuseDraft(row)) return
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
    // Re-enabled every time, because `onStarted` turns it off imperatively and that
    // assignment outlives the process. Quickshell closes the write channel before
    // `start()` when this is false, so a second attempt would silently drop the token
    // and report "could not sign in" for a token that was never sent.
    loginProcess.stdinEnabled = true
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

  // The account's tags, for the filter picker. Read once at startup and again
  // whenever a write settles, because that is when a new one can have appeared.
  function loadTags() {
    if (tagsProcess.running) return
    tagsProcess.command = ["python3", helperPath, "tags"]
    tagsProcess.running = true
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

  // A row the widget invented moments ago and the helper has not confirmed. Its id is
  // a placeholder no endpoint can resolve, so acting on it would send a request that
  // 404s — and, because a failed write only lands in the outbox's warning, would look
  // like it worked. Refusing for the second it takes to settle is the honest answer.
  function draftRow(row) {
    return !!row && String(row.id || "").indexOf("local-draft-") === 0
  }

  function refuseDraft(row) {
    if (!draftRow(row)) return false
    toast("Still saving — try again in a moment")
    return true
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
    // A note was never counted, so un-counting one makes the badge read low until the
    // next refresh. Guarded here rather than at each caller: `complete` refuses notes
    // outright, but `remove` does not — deleting a note is a perfectly ordinary thing.
    if (Model.isNote(gone)) return
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
      if (Array.isArray(body.groups)) groups = body.groups
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
      // `ok` means the change is durable, not that it reached TickTick. A write the
      // server refused, or one still queued behind a lapsed token, says so here — and
      // saying nothing would leave the user believing a task exists that does not.
      var warning = Model.squish(payload.warning)
      if (warning !== "") toast(warning)
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

  onPausedChanged: {
    if (paused || !_refreshQueued) return
    _refreshQueued = false
    Qt.callLater(root.refresh)
  }

  onViewChanged: Qt.callLater(root.refresh)
  onProjectFilterChanged: Qt.callLater(root.refresh)
  onSortChanged: Qt.callLater(root.refresh)
  onPriorityFilterChanged: Qt.callLater(root.refresh)
  onTagFilterChanged: Qt.callLater(root.refresh)
  onSearchChanged: searchTimer.restart()

  Component.onCompleted: root.loadTags()

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
      parseProcess.forText = pending
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
      else Qt.callLater(function () { root.settled() })
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
    // Its own slot for the same reason the preview has one: a tag list is
    // decoration and must never delay a click.
    id: tagsProcess
    running: false
    command: []
    stdout: StdioCollector { id: tagsOut; waitForEnd: true }
    onExited: function () {
      var payload = root.payloadOf(String(tagsOut.text || ""))
      var body = root.bodyOf(payload)
      if (payload && payload.ok === true && Array.isArray(body.tags)) root.tags = body.tags
    }
  }

  Process {
    // Its own slot: the preview must never wait behind a write, nor cancel one.
    id: parseProcess
    running: false
    command: []
    stdout: StdioCollector { id: parseOut; waitForEnd: true }
    property string forText: ""
    onExited: function () {
      var payload = root.payloadOf(String(parseOut.text || ""))
      var body = root.bodyOf(payload)
      var good = payload && payload.ok === true
      root._preview = good ? Model.parseSummary(body) : ""
      // Kept alongside the summary so `add` can title its optimistic row with the
      // parsed title rather than the raw line the user typed.
      root._previewTitle = good ? Model.squish((body.parsed || body).title) : ""
      root._previewFor = good ? parseProcess.forText : ""
    }
  }
}
