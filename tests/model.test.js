// Unit tests for qml/Model.js — the presentation helpers the QML binds to.
//
// These are pure, total functions with no Qt in them, which is exactly why they are
// worth testing outside Qt: `tests/qml_smoke.sh` needs a live Wayland session and an
// Omarchy install, so CI cannot run it, and until now nothing checked this file at all.
// A wrong answer here renders as a blank row rather than an error.
//
// Run: node --test tests/
//
// Model.js is loaded rather than `require`d because it opens with QML's `.pragma
// library` directive, which is not JavaScript. Stripping that one line is the whole
// difference between the two runtimes.

const test = require("node:test")
const assert = require("node:assert")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

function loadModel() {
  const file = path.join(__dirname, "..", "qml", "Model.js")
  const src = fs.readFileSync(file, "utf8").replace(/^\s*\.pragma\s+library\s*$/m, "")
  // `Qt` is only touched by the colour helpers, which are not under test here.
  const sandbox = { module: { exports: {} }, Qt: undefined }
  vm.runInNewContext(src, sandbox, { filename: file })
  return sandbox.module.exports
}

const Model = loadModel()

// Arrays that come back out of the sandbox have the sandbox's Array prototype, so
// deepStrictEqual would compare prototypes and fail on values that are identical.
const plain = (value) => Array.from(value)

// A stand-in for the array-like a Repeater hands its delegate: indexable, with a
// length, `constructor.name === "Array"` — and `Array.isArray()` false. This is what
// a nested array really looks like after crossing the QVariant boundary, and guarding
// on Array.isArray silently threw away every checklist and every reminder.
function sequence(items) {
  const wrapper = Object.create({ constructor: Array })
  items.forEach((item, i) => { wrapper[i] = item })
  wrapper.length = items.length
  return wrapper
}

test("asList accepts a real array unchanged", () => {
  const list = [1, 2, 3]
  assert.strictEqual(Model.asList(list), list)
})

test("asList unwraps an array-like that fails Array.isArray", () => {
  const wrapped = sequence(["a", "b"])
  assert.strictEqual(Array.isArray(wrapped), false, "the fixture must not be a real array")
  assert.deepStrictEqual(plain(Model.asList(wrapped)), ["a", "b"])
})

test("asList is total", () => {
  for (const value of [undefined, null, 0, 42, true, "abc", {}, { length: "no" }]) {
    assert.deepStrictEqual(plain(Model.asList(value)), [], `for ${JSON.stringify(value)}`)
  }
})

test("reminders render through an array-like, not just an array", () => {
  assert.strictEqual(Model.remindersLabel(sequence(["TRIGGER:-PT30M"])), "30 min before")
  assert.strictEqual(Model.remindersLabel(sequence(["TRIGGER:PT0S", "TRIGGER:-PT1H"])),
                     "At time +1")
  assert.strictEqual(Model.remindersLabel(sequence([])), "")
})

test("reminder offsets read the way a person would say them", () => {
  assert.strictEqual(Model.reminderLabel("TRIGGER:PT0S"), "At time")
  assert.strictEqual(Model.reminderLabel("TRIGGER:-PT60M"), "1 hour before")
  // The all-day form counts from midnight, so the day offset and the clock time are
  // two different facts: -P0DT15H0M0S is 15 hours before midnight, i.e. 09:00 the
  // day before. This is exactly what `--remind 1d` writes for an all-day task.
  assert.strictEqual(Model.reminderLabel("TRIGGER:-P0DT15H0M0S"), "1 day before, 09:00")
  assert.strictEqual(Model.reminderLabel("TRIGGER:-P1DT15H0M0S"), "2 days before, 09:00")
  assert.strictEqual(Model.reminderLabel(""), "")
  assert.strictEqual(Model.reminderLabel("nonsense"), "Reminder")
})

test("repeat rules name only the shapes worth naming", () => {
  assert.strictEqual(Model.repeatLabel("RRULE:FREQ=DAILY"), "Daily")
  assert.strictEqual(Model.repeatLabel("RRULE:FREQ=WEEKLY;INTERVAL=2"), "Every 2 weeks")
  assert.strictEqual(Model.repeatLabel("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"), "Weekdays")
  assert.strictEqual(Model.repeatLabel("ERULE:NAME=CUSTOM"), "Custom")
  assert.strictEqual(Model.repeatLabel(""), "")
})

test("a note is a note however the row spells it", () => {
  assert.strictEqual(Model.isNote({ isNote: true }), true)
  assert.strictEqual(Model.isNote({ kind: "NOTE" }), true)
  assert.strictEqual(Model.isNote({ kind: "note" }), true)
  assert.strictEqual(Model.isNote({ kind: "TEXT" }), false)
  assert.strictEqual(Model.isNote({}), false, "a task with no kind field is a to-do")
  assert.strictEqual(Model.isNote(undefined), false)
})

// The checkbox, the strikethrough and the guard that refuses to "complete" finished
// work all hang off this one answer. An open task reading as done is what the cursor
// used to fake; a done task reading as open re-completes it on Enter.
test("done is read from status or from the completed bucket", () => {
  assert.strictEqual(Model.isDone({ status: 2 }), true)
  assert.strictEqual(Model.isDone({ status: "2" }), true, "a row can arrive stringly typed")
  assert.strictEqual(Model.isDone({ status: 0, bucket: "completed" }), true,
    "the completed endpoint's rows are stamped by the helper, whatever status says")
  assert.strictEqual(Model.isDone({ status: 0, bucket: "today" }), false)
  assert.strictEqual(Model.isDone({}), false, "a row with no status is open")
  assert.strictEqual(Model.isDone(undefined), false)
})

test("sections break on identity and are headed by name", () => {
  const a = { projectId: "p1", project: "Work", bucket: "today" }
  const b = { projectId: "p2", project: "Work", bucket: "today" }
  // Two lists really can share a name; grouping on the name would merge them.
  assert.notStrictEqual(Model.sectionKey(a, "list"), Model.sectionKey(b, "list"))
  assert.strictEqual(Model.sectionLabel(a, "list"), "Work")
  assert.strictEqual(Model.sectionKey(a, "time"), Model.sectionKey(b, "time"))
  assert.strictEqual(Model.sectionLabel(a, "time"), "Today")
  // Sorted by priority or title, any heading would cut across the order asked for.
  assert.strictEqual(Model.sectionKey(a, "priority"), "")
  assert.strictEqual(Model.sectionLabel(a, "title"), "")
  assert.strictEqual(Model.sectionLabel({ projectId: "" }, "list"), "No list")
})

test("tags round-trip through one text field", () => {
  assert.deepStrictEqual(plain(Model.tagList("#work, home")), ["work", "home"])
  assert.deepStrictEqual(plain(Model.tagList("  ##urgent   urgent ")), ["urgent"], "deduped")
  assert.deepStrictEqual(plain(Model.tagList("")), [])
  assert.strictEqual(Model.tagText(["work", "home"]), "#work #home")
  assert.strictEqual(Model.tagText(sequence(["work"])), "#work")
  assert.strictEqual(Model.tagText(undefined), "")
})

test("a date field is seeded with something that cannot drift", () => {
  // Not the human label: re-committing "Mon" would move the task to *next* Monday.
  assert.strictEqual(Model.dateSeed("2026-08-15T09:00:00+02:00", false), "2026-08-15 09:00")
  assert.strictEqual(Model.dateSeed("2026-08-15T00:00:00+02:00", true), "2026-08-15")
  assert.strictEqual(Model.dateSeed("", false), "")
  assert.strictEqual(Model.dateSeed(undefined, false), "")
})

test("checklist progress reads x/y, and nothing when there is no checklist", () => {
  assert.strictEqual(Model.checklistProgress(1, 3), "1/3")
  assert.strictEqual(Model.checklistProgress(0, 0), "")
  assert.strictEqual(Model.checklistProgress(9, 3), "3/3", "clamped, never 9/3")
})

test("every helper survives being handed nothing", () => {
  assert.strictEqual(Model.squish(undefined), "")
  assert.strictEqual(Model.elide(undefined, 10), "")
  assert.strictEqual(Model.toInt("not a number"), 0)
  assert.strictEqual(Model.priorityLabel(undefined), "")
  assert.strictEqual(Model.sectionTitle(undefined), "Tasks")
  assert.strictEqual(Model.parseSummary(undefined), "")
})
