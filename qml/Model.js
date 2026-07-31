.pragma library

// Presentation helpers for the TickTick widget. Pure, stateless, and total:
// every function returns a usable string or colour for any input, including
// undefined — a row that renders "undefined" is worse than one that renders "".

var BUCKET_TITLES = {
  overdue: "Overdue",
  today: "Today",
  tomorrow: "Tomorrow",
  upcoming: "Upcoming",
  later: "Later",
  undated: "No date"
}

var PRIORITY_LABELS = { 5: "High", 3: "Medium", 1: "Low" }

var FREQ_UNITS = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year", HOURLY: "hour" }
var FREQ_EVERY = { DAILY: "Daily", WEEKLY: "Weekly", MONTHLY: "Monthly", YEARLY: "Yearly", HOURLY: "Hourly" }

var WEEKDAYS = ["MO", "TU", "WE", "TH", "FR"]

function squish(text) {
  return String(text === undefined || text === null ? "" : text).replace(/\s+/g, " ").replace(/^ | $/g, "")
}

function toInt(value) {
  var n = parseInt(String(value === undefined || value === null ? "" : value), 10)
  return isFinite(n) ? n : 0
}

// A real JS array for anything array-shaped.
//
// NOT `Array.isArray`. A Repeater over a `var` array hands each delegate its row as a
// QVariant, and a nested array inside it arrives as a *sequence wrapper*: indexable,
// with a length, and `constructor.name === "Array"` — but `Array.isArray()` says false.
// Guarding on that therefore threw away every checklist and every reminder the moment
// a row was read through `modelData`, which is the only way rows are ever read.
function asList(value) {
  if (value === undefined || value === null) return []
  if (Array.isArray(value)) return value
  // Strings are array-like too, and a string of characters is never what was meant.
  if (typeof value !== "object" || typeof value.length !== "number") return []
  var out = []
  for (var i = 0; i < value.length; i++) out.push(value[i])
  return out
}

function elide(text, max) {
  var value = squish(text)
  var limit = toInt(max)
  if (limit <= 1 || value.length <= limit) return value
  return value.substring(0, limit - 1) + "…"
}

function sectionTitle(bucket) {
  var key = squish(bucket).toLowerCase()
  if (BUCKET_TITLES[key]) return BUCKET_TITLES[key]
  if (key === "") return "Tasks"
  return key.charAt(0).toUpperCase() + key.substring(1)
}

// What a row is grouped under, and what decides where a group breaks. The two
// differ when grouping by list: two lists can share a name, so the break is keyed
// on the id and only the heading uses the name. Sorting by priority or title has
// no grouping — any heading would cut across the very order that was asked for.
function sectionKey(row, sort) {
  var r = row || {}
  if (squish(sort) === "list") return "list:" + String(r.projectId || "")
  if (squish(sort) === "time") return "time:" + String(r.bucket || "undated")
  return ""
}

function sectionLabel(row, sort) {
  var r = row || {}
  if (squish(sort) === "list") return squish(r.project) || "No list"
  if (squish(sort) === "time") return sectionTitle(r.bucket)
  return ""
}

// A note is something to read, not something to do: TickTick gives it no checkbox,
// so neither do we, and nothing counts it.
function isNote(row) {
  var r = row || {}
  if (r.isNote === true) return true
  return String(r.kind || "").toUpperCase() === "NOTE"
}

// Finished work. `status` is TickTick's own flag — 0 is open, 2 is done, and the
// abandoned states are also not open — and rows fetched from the completed endpoint
// are stamped `bucket: "completed"` by the helper regardless of what status says.
// Either one on its own is enough; a row that is done must never paint as open.
function isDone(row) {
  var r = row || {}
  if (toInt(r.status) > 0) return true
  return squish(r.bucket).toLowerCase() === "completed"
}

function priorityLabel(priority) {
  return PRIORITY_LABELS[toInt(priority)] || ""
}

// High burns urgent, medium and low walk back toward the bar foreground, and
// "no priority" is plain text — the ramp is derived so it tracks the theme.
function priorityColor(priority, foreground, urgent) {
  var p = toInt(priority)
  if (p >= 5) return urgent || foreground
  if (p >= 3) return mix(foreground, urgent, 0.6)
  if (p >= 1) return mix(foreground, urgent, 0.25)
  return foreground
}

function mix(from, to, amount) {
  var a = asColor(from)
  var b = asColor(to)
  if (!a) return to || from
  if (!b) return from
  var k = Math.max(0, Math.min(1, Number(amount)))
  if (!isFinite(k)) return from
  try {
    return Qt.rgba(a.r + (b.r - a.r) * k, a.g + (b.g - a.g) * k, a.b + (b.b - a.b) * k, a.a + (b.a - a.a) * k)
  } catch (e) {
    return from
  }
}

function asColor(value) {
  if (!value) return null
  if (typeof value === "object" && typeof value.r === "number") return value
  try {
    return Qt.color(String(value))
  } catch (e) {
    return null
  }
}

// repeatFlag is not reliably RFC-5545 (TT_SKIP=…, ERULE:NAME=CUSTOM, unordered
// BYDAY), so match only the shapes worth naming and call everything else what
// it is: something that repeats.
function repeatLabel(flag) {
  var text = squish(flag).toUpperCase()
  if (text === "") return ""
  var freq = /FREQ=([A-Z]+)/.exec(text)
  if (!freq) return "Custom"
  var name = freq[1]
  var interval = toInt((/INTERVAL=(\d+)/.exec(text) || [])[1]) || 1
  var byday = (/BYDAY=([A-Z,\-+0-9]+)/.exec(text) || [])[1] || ""
  if (name === "WEEKLY" && interval === 1 && isWeekdaySet(byday)) return "Weekdays"
  if (!FREQ_UNITS[name]) return "Repeats"
  if (interval === 1) return FREQ_EVERY[name]
  return "Every " + interval + " " + FREQ_UNITS[name] + "s"
}

function isWeekdaySet(byday) {
  var days = squish(byday).toUpperCase().split(",")
  if (days.length !== WEEKDAYS.length) return false
  for (var i = 0; i < WEEKDAYS.length; i++) {
    if (days.indexOf(WEEKDAYS[i]) < 0) return false
  }
  return true
}

// Reminders are RFC-5545 TRIGGER offsets from the task's date; for all-day
// tasks the offset runs from midnight, which is why "-P0DT15H0M0S" is 09:00
// the day before rather than fifteen hours before anything.
function reminderLabel(reminder) {
  var text = squish(typeof reminder === "object" && reminder ? (reminder.trigger || reminder.id) : reminder).toUpperCase()
  if (text === "") return ""
  var body = text.replace(/^TRIGGER[:;=]/, "")
  var parts = /^(-)?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(body)
  if (!parts) return "Reminder"
  var before = parts[1] === "-"
  var minutes = toInt(parts[2]) * 1440 + toInt(parts[3]) * 60 + toInt(parts[4]) + (toInt(parts[5]) >= 30 ? 1 : 0)
  if (minutes === 0) return "At time"
  if (parts[2] === undefined) return spanText(minutes) + (before ? " before" : " after")
  // All-day form: the day offset and the clock time are two different facts.
  var days = before ? Math.ceil(minutes / 1440) : Math.floor(minutes / 1440)
  var clock = before ? (1440 - (minutes % 1440)) % 1440 : minutes % 1440
  var when = days === 0 ? "On the day" : days + (days === 1 ? " day " : " days ") + (before ? "before" : "after")
  return when + ", " + clockText(clock)
}

function remindersLabel(reminders) {
  var list = asList(reminders)
  var first = list.length > 0 ? reminderLabel(list[0]) : ""
  if (first === "") return ""
  return list.length > 1 ? first + " +" + (list.length - 1) : first
}

function spanText(minutes) {
  var m = toInt(minutes)
  if (m % 1440 === 0) return plural(m / 1440, "day")
  if (m % 60 === 0) return plural(m / 60, "hour")
  return m + " min"
}

function clockText(minutes) {
  var m = ((toInt(minutes) % 1440) + 1440) % 1440
  return pad2(Math.floor(m / 60)) + ":" + pad2(m % 60)
}

function pad2(value) {
  var n = toInt(value)
  return (n < 10 ? "0" : "") + n
}

function plural(count, noun) {
  var n = toInt(count)
  return n + " " + noun + (n === 1 ? "" : "s")
}

// Tags round-trip through a single text field: "#work, home" in, ["work","home"]
// out. The leading # is optional because people type it either way, and TickTick
// stores the bare name.
function tagList(text) {
  var parts = squish(text).split(/[,\s]+/)
  var out = []
  for (var i = 0; i < parts.length; i++) {
    var tag = parts[i].replace(/^#+/, "")
    if (tag !== "" && out.indexOf(tag) < 0) out.push(tag)
  }
  return out
}

function tagText(tags) {
  var list = asList(tags)
  var out = []
  for (var i = 0; i < list.length; i++) {
    var tag = squish(list[i])
    if (tag !== "") out.push("#" + tag)
  }
  return out.join(" ")
}

// What to put in an editable date field so committing it unchanged is a no-op.
// The row's `dueLabel` is written for reading ("Mon 14:30", "15 Aug") and re-parsing
// that would quietly move a date to the *next* Monday; an explicit calendar date
// cannot drift. Both spellings round-trip through the same parser quick-add uses.
function dateSeed(iso, allDay) {
  var text = squish(iso)
  var parts = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?/.exec(text)
  if (!parts) return ""
  if (allDay || parts[2] === undefined) return parts[1]
  return parts[1] + " " + parts[2] + ":" + parts[3]
}

function checklistProgress(done, total) {
  var all = toInt(total)
  if (all <= 0) return ""
  return Math.max(0, Math.min(all, toInt(done))) + "/" + all
}

// Human read-back of `ticktick parse`, so quick-add can show what it understood
// before the task exists. Key names are taken defensively: the helper's JSON
// may nest under `parsed` and may spell all-day either way.
function parseSummary(payload) {
  var p = (payload && typeof payload === "object" ? (payload.parsed || payload) : {}) || {}
  if (typeof p.preview === "string") return squish(p.preview)
  var parts = []
  var title = elide(p.title, 48)
  if (title !== "") parts.push(title)
  var due = squish(p.dueLabel || p.due_label || p.due || p.dueIso || p.due_iso)
  if (due !== "") parts.push(due)
  var priority = priorityLabel(p.priority)
  if (priority !== "") parts.push(priority)
  var project = squish(p.project)
  if (project !== "") parts.push("#" + project)
  var repeat = repeatLabel(p.repeat || p.repeatFlag || p.repeat_flag)
  if (repeat !== "") parts.push(repeat)
  var reminders = remindersLabel(p.reminders)
  if (reminders !== "") parts.push(reminders)
  return parts.join(" · ")
}

if (typeof module !== "undefined") {
  module.exports = {
    squish: squish,
    toInt: toInt,
    asList: asList,
    elide: elide,
    sectionTitle: sectionTitle,
    sectionKey: sectionKey,
    sectionLabel: sectionLabel,
    isNote: isNote,
    isDone: isDone,
    tagList: tagList,
    tagText: tagText,
    dateSeed: dateSeed,
    priorityLabel: priorityLabel,
    priorityColor: priorityColor,
    mix: mix,
    repeatLabel: repeatLabel,
    isWeekdaySet: isWeekdaySet,
    reminderLabel: reminderLabel,
    remindersLabel: remindersLabel,
    checklistProgress: checklistProgress,
    parseSummary: parseSummary
  }
}
