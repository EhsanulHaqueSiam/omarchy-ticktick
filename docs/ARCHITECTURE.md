# Architecture

## The one structural decision

**QML never speaks HTTP.**

```
┌─ omarchy-shell (one long-running Quickshell process) ──────────────┐
│                                                                     │
│  qml/BarWidget.qml     bar button + popup, cursor model, key map    │
│    ├─ qml/TaskRow.qml       one row                                 │
│    ├─ qml/TaskDetail.qml    expanded body, checklist items          │
│    ├─ qml/QuickAdd.qml      natural-language input + parse preview  │
│    ├─ qml/Model.js          pure presentation helpers               │
│    └─ qml/Service.qml       state, process orchestration, queueing  │
│                    │                                                │
└────────────────────┼────────────────────────────────────────────────┘
                     │  Quickshell Process + StdioCollector
                     │  argv in, exactly one JSON object out
                     ▼
              bin/ticktick                    (stdlib Python 3.11+)
                     │
   ┌─────────────────┼──────────────────────────────────────────┐
   │ ticktick/cli.py         argparse dispatch, JSON contract    │
   │ ticktick/store.py       local cache + durable outbox        │
   │ ticktick/views.py       fetch → bucket → group → sort → row │
   │ ticktick/render.py      the same payloads, for a person     │
   │ ticktick/api.py         one method per endpoint, retries    │
   │ ticktick/nlp.py         natural-language quick add          │
   │ ticktick/dates.py       parsing, all-day rules, bucketing   │
   │ ticktick/auth.py        API-token sign-in, OAuth2 as backup │
   │ ticktick/config.py      credential store (atomic, 0600)     │
   │ ticktick/errors.py      the error kinds the CLI reports     │
   └──────────┬──────────────────────────┬──────────────────────┘
              │                          │  HTTPS
              ▼                          ▼
   $XDG_STATE_HOME/ticktick/     api.ticktick.com/open/v1
   state.json (cache + outbox)
```

Why draw the line there:

- **Everything worth testing becomes testable.** Date bucketing, the language parser, grouping
  and sorting run under `unittest` with an injected clock. Testing the same logic inside QML
  would need a running compositor.
- **The whole data path is debuggable from a terminal.** When the widget is wrong, you run
  `./bin/ticktick tasks` and see exactly what it saw. No instrumentation, no shell restart.
- **The user gets a CLI for free**, which is genuinely useful on its own.
- **A crash cannot take the bar down.** The helper is a subprocess. QML holds a `Process` and a
  parser; the worst a broken helper can do is leave the last-known-good list on screen.

The cost is process spawn latency per action (~150ms). For a bar widget that polls every five
minutes and reacts to occasional clicks, that is free.

## Local-first

The second structural decision, and the one that shaped `store.py`. Every CLI invocation is a
fresh process, so an optimistic edit held only in QML died at the next poll, and a poll with no
network blanked the list. Both are the same missing thing: **state that outlives the process**.

One JSON file at `$XDG_STATE_HOME/ticktick/state.json` (0600, written atomically) holds the raw
payloads of the last good fetch and an outbox of mutations that have been applied locally but
not yet accepted upstream:

```
read    state.json ──► outbox replayed on top ──► views.collect ──► rows
write   apply to cache ──► append to outbox ──► try to flush ──► drop what stuck
```

- **The cache holds raw API payloads, never rendered rows.** A row carries a bucket and a label
  computed against the clock at fetch time, so a cached row is confidently wrong by morning.
  Caching what the API said and re-deriving the view on every read is the only version that is
  right at 00:01.
- **`store.CachedSource` answers the same four methods `views.collect` calls on `api.Client`**,
  so the view layer never learns whether it is online, and nothing about caching leaks into it.
- **A read is always served from the cache; the network only ever updates it.** `_read_source`
  refetches when the cache is older than 45 seconds (`--max-age` tunes it, `--refresh` forces a
  fetch, `--offline` forbids one). A dead link, an expired token or a rate limit therefore
  degrades to the last known-good list with a `warning` attached, never to an empty widget.
- **A write is on screen before it is sent and durable before the process exits.** A task
  created with no network gets a `local-` id; when the create finally lands, the server's copy
  replaces the draft and every still-queued entry holding the placeholder is rewritten to the
  real id. Checklist items work the same way — a locally minted item id is stripped from the
  wire body so TickTick assigns a real one, and the update response is adopted for it.
- **`flush` stops at the first entry that fails for a reason retrying could fix**, so ordering
  holds: an edit must never overtake the create it depends on.
- **A 4xx drops the queued entry; a 5xx keeps it.** A body the server refuses on its own terms
  will never be accepted, and retrying it forever wedges every write behind it. A server-side
  failure is the opposite: discarding the entry would throw away a task the user already watched
  themselves complete. Both are bounded — eight transport attempts per entry, 500 entries in the
  outbox, oldest dropped first because the newest are what is still on screen.
- **The rate-limit backoff is persisted, not held in memory.** Every CLI call is a new process,
  so an in-memory retry timer would let a widget polling every 30 seconds hammer straight
  through a limit it had already been told about.

`logout` deletes the state file along with the credentials: it holds task titles, and those are
private even though no token is stored in it.

## The JSON contract

Every subcommand except `auth` and `selftest` prints **exactly one JSON object to stdout and
exits 0** — including on failure:

```json
{"ok": true,  "tasks": [...], "projects": [...], "counts": {...}}
{"ok": false, "error": "auth", "message": "not signed in — run `ticktick login`"}
```

`error` is one of `auth`, `network`, `api`, `usage`, `notfound`. The QML side branches on it:
`auth` swaps in the sign-in affordance; anything else sets an error line and **keeps the data
already on screen**.

A non-zero exit or a traceback on stdout would leave the parser with nothing and blank the
widget, which is a worse failure than a stale list. `cli.main()` therefore catches
`TickTickError` by kind and bare `Exception` as a last resort.

Library modules never print. Only `cli.py` writes to stdout; `auth.py` routes its human-facing
prose through a callback the CLI sends to **stderr**, so even the interactive command keeps
stdout clean.

The same payload is rendered twice. `render.py` formats it for a terminal, and the choice is
made by asking whether stdout is a TTY rather than by a flag — QML always redirects stdout and
therefore always gets JSON, with nothing to remember and nothing to break when a new call site
is added. `--json` and `--text` force either shape. `render.render` never raises: a formatting
bug falls back to pretty-printed JSON rather than turning a successful command into a failed one.

## Row shape

`views.collect()` emits rows with a fixed key set, every key always present and never null:

```
id projectId project projectColor title content bucket due dueIso dueLabel
priority isAllDay repeat reminders items itemsDone itemsTotal
tags start startLabel parentId sortOrder status progress pending
```

`due` is epoch seconds (`0` when undated), `dueIso` the local ISO string. Missing optional API
fields become `""` / `0` / `[]` rather than absent, because a missing key renders as the string
`undefined` in a QML binding. Buckets are `overdue`, `today`, `tomorrow`, `upcoming`, `later`,
`undated`, ordered that way in the UI.

The second block is what the live API returns and its documentation omits — tags, a start date
as well as a due date, `parentId` for real nested subtasks, `sortOrder`, `status` and
`progress`. They cost nothing to carry and the UI cannot show what it never receives. `pending`
is local truth rather than API data: it marks a row whose change has not reached TickTick yet.

`counts` is computed over **every fetched task before** view/search/priority/tag filtering, so
the bar badge does not flicker while the user types in the search box.

Every read also carries `source` (`cache` or `network`), `age`, `stale` and `pending`, plus a
`warning` when the refresh failed and the cache answered instead.

## Signing in

The primary path is a **personal API token** from the TickTick web app, under
Settings → Account → API Token. It is an ordinary bearer token — the same thing the OAuth dance
produces, minus the app registration, the client secret, the redirect URI that must match
character-for-character, and a free port on localhost. Nobody should have to visit a developer
console to see their own todos. `ticktick auth` keeps the browser flow for those who prefer it.

The token is read from **stdin, never argv**. Anything in argv is world-readable through
`/proc/<pid>/cmdline` for as long as the process lives, and this token is a full account
credential. `Service.qml` enables stdin on the sign-in `Process` only, writes the token, then
closes stdin so the helper's `read()` returns instead of blocking until its timeout.

`login` verifies the token with one cheap read before storing anything. An unverified token
would move the failure from sign-in, where it can be explained, to the next refresh, where it
surfaces as an empty widget.

If nothing is signed in here but TickTick's own CLI is signed in on this machine
(`~/.config/ticktick-cli/config.json`), its token is borrowed. Both tools authenticate the same
user against the same API with the same kind of token, so asking again would be busywork. That
file is only ever read. `logout` records `adopt_external: false` so that signing out stays
signed out rather than silently re-borrowing on the next call.

## Service.qml

- **Four `Process` objects** — reads, writes, sign-in and the quick-add parse preview — so a
  slow refresh never blocks a click, a pasted token is never queued behind a poll, and a preview
  can neither wait for nor cancel a write. Only the sign-in process has stdin enabled.
- **A serialized write queue.** Two fast clicks must not race one process slot; actions queue,
  pump on exit through `Qt.callLater`, and trigger a single refresh when the queue drains.
- **Optimistic updates.** Complete, delete, priority and checklist toggles mutate the local list
  immediately, then reconcile against the refresh that follows. A failed action is corrected by
  truth arriving, not by the UI guessing. The helper's own cache makes this durable: an
  optimistic edit now survives the poll that used to undo it.
- **Stale-while-revalidating.** A refresh in flight never blanks the list; `tasks` is replaced
  only when a successful payload lands.

## Host contracts

Three properties of the Omarchy bar that this widget is written against. All of them fail
silently when broken, which is why each is stated in code as well as here.

- **`ipcTarget` is the full plugin id, `siam.ticktick`, not a short name.** An IPC target is
  single-occupancy: the first handler to register wins and every later one is silently dead. A
  generic `ticktick` collides with any other TickTick plugin or an older copy of this one, which
  was observed live. `Panel`'s built-in handler is also turned off (`manageIpc: false`), because
  it only offers open/close/toggle and a target accepts exactly one handler.
- **Settings arrive as strings.** `omarchy bar plugin set <id> <key> <value>` writes a string
  unless the caller remembers `--json`, so a boolean setting arrives as `"true"` far more often
  than as `true`; comparing with `=== true` makes the setting appear to save and then do
  nothing. Every boolean goes through `boolSetting`. The manifest's `defaults` block is inert at
  runtime, so each default lives in its `setting(key, fallback)` call.
- **The bar is instantiated once per monitor**, but IPC routes to one instance, so every IPC
  mutation fans out over `bar.moduleWidgets`.

## What verifies the QML, and why qmllint is not enough

`qmllint` is a syntax gate and nothing more. The `qs.Ui`, `qs.Commons` and `Quickshell.*` import
namespaces only exist inside a running Omarchy shell, so unresolved imports and every type error
that follows from them are expected off-box; CI fails only on parse errors.

`tests/validate_qml_api.py` covers what the linter cannot. `Style.spacing` and `Style.font` are
declared as inline `QtObject`s, so `qmllint` reports "member not found" for a perfectly good
member and for a typo alike — useless output for exactly the mistakes that are easiest to make.
A misspelt `Style.spacing.padding` costs nothing at load time and renders as 0: a layout subtly
wrong on someone else's machine and fine on yours. The member lists live in that file, and are
re-derived from a real install when one is present so they cannot rot unnoticed.

`tests/qml_smoke.sh` loads the widget in a real Quickshell against the fake API server. It
exists because two real bugs got past `qmllint` and were caught only by running the thing:
`function escape()` is an illegal QML method name, and `height: visible ? implicitHeight : 0` on
a wrapping `Text` is a binding loop. The script also asserts that string settings are coerced to
booleans and that the Today view holds exactly the overdue and today rows. It needs a live
Wayland session and an Omarchy install, so it runs on the box before a release rather than in
CI, and it only ever touches temp directories.

## Things the live API does that its documentation does not say

Each of these was verified by executing against a real account. They are the reason several
parts of this code look more paranoid than they need to.

| Behaviour | Consequence in this codebase |
|---|---|
| Responses carry **milliseconds** (`…T18:00:00.000+0000`), not the documented `yyyy-MM-dd'T'HH:mm:ssZ` | `dates.parse` uses `datetime.fromisoformat` first and only falls back to `strptime`. Parsing with the documented format alone raises on every date, and a `None`-returning parser then renders every task as undated — silently. |
| All-day due dates are **midnight in the task's own `timeZone`** | `dates.parse` resolves all-day tasks through `ZoneInfo(task["timeZone"])`. Using UTC or the viewer's zone shifts every all-day task by a day for anyone far enough from UTC. |
| An all-day task is not late until the day is over | `dates.bucket` never marks an all-day task `overdue` on its own date. |
| A **bare date string is silently discarded** — `{"dueDate": "2026-08-06"}` returns 200 and comes back `null` | Every date reaching the wire goes through `_wire_date`, which refuses anything without a UTC offset. |
| `POST /task/filter` returns **every task in one request**, Inbox included | `Client.all_undone()` is one call. The per-project fan-out survives only as a fallback. |
| Rate limiting is **HTTP 500 with `exceed_query_limit` in the body**, not 429 | The retry layer sniffs the body, not just the status, and backs off on seconds rather than milliseconds. The backoff is then written to the state file, because every CLI call is a new process and an in-memory timer would be forgotten between them. |
| The **Inbox is absent from `GET /project`** but `GET /project/inbox/data` works | The catalogue synthesises an Inbox entry; no create-and-delete discovery probe touches the user's account. |
| `repeatFlag` is **not reliably RFC-5545** — `TT_SKIP=WEEKEND`, `TT_WORKDAY=-1`, `ERULE:NAME=CUSTOM;BYDATE=…` all occur | It is never parsed for logic. `Model.js` pattern-matches a few common forms for a label and falls back to "Repeats"; the string round-trips verbatim on update. |
| `/project/{id}/data` returns **undone tasks only** | Completed work comes from `POST /task/completed`, and a finished task can only be found by name through `POST /task/search`. |
| Moving a project has a **real endpoint** (`POST /task/move`, array body) | Rewriting `projectId` through the update endpoint is not a supported path and does not move the task. |
| **`GET /tag` answers 200** although the published API documents no tag endpoint at all | `ticktick tags`, the `--tag` filters, and `tags` on every row. `name` is the lowercase key a task's `tags` array holds, `label` is what the user typed and what is displayed, and `parent` names another tag's `name`, so tags form a tree. |
| **`GET /project/group` answers 200** | `ticktick project groups`. Projects sit in folders, which the documented project schema never mentions. |
| **`POST /task/search` answers 200** and reaches **completed** tasks, unlike `/task/filter` | It is its own verb rather than a filter over the cached list, because it is the only search that can answer "where did I put that". `keywords` and `status` are honoured; `projectIds`, `dueFrom` and `dueTo` are accepted and then **ignored** — the same query returns the same rows with and without them — so nothing here may rely on them. |
| **`GET /project/{id}/column` answers 200** | `Client.columns`. Kanban columns exist and are empty for a list project. |
| **`GET /habit` and `GET /countdown` answer 200** | Nothing here uses them. Recorded so the next person does not have to rediscover that the surface is wider than the docs. |
| Task objects carry **`tags`, `parentId`/`childIds`, `sortOrder`, `startDate`, `progress`, `columnId`, `etag`, `kind`** — none of which appear in the documented task schema | `views.row` carries the ones the UI can use. In particular `parentId` means nested subtasks are real, not just the flat `items[]` checklist. |
| Optional fields are **omitted, not sent as null** — a task with no tags has no `tags` key at all | Every reader treats "missing" and "empty" identically; distinguishing them would make an untagged task an error case. |

## Parser design

`nlp.parse` is deliberately **conservative**: it is far worse to mangle a title than to miss a
date. Every rule is anchored on word boundaries and requires a standalone token, so
`call #1 supplier`, `meeting at 5 people` and `review PR !important` all survive intact. Only
what was actually consumed is stripped, and the consumed substrings are returned in `matched` so
the quick-add field can show a live preview of its own interpretation.

Relative dates are computed from an **injected** `now`, never the wall clock — `dates.now()`
exists as a single seam so the test suite can freeze time and stay deterministic forever.

New parser rules need negative cases as well as positive ones; leaving unrecognised text alone
is the behaviour that regresses.

## Testing

165 tests under `unittest`, no third-party dependencies.

The end-to-end suite drives `cli.main(argv)` exactly as `bin/ticktick` does, against
`tests/fake_ticktick.py` — a stand-in TickTick served over real HTTP on loopback. Talking real
HTTP rather than stubbing `Client` is the point: argument parsing, the JSON contract, the retry
layer, throttle detection, the credential store, the cache and the outbox all stay inside the
test. The fake reproduces the misbehaviours above, including the throttle-flavoured 500 and the
silently-discarded bare date, and can be told to go offline, fail, throttle or answer garbage on
demand. Only two paths are redirected: the credential store and the state file, both into a temp
directory, so a test can never read or clobber a real account.

`tests/validate_manifest.py` re-implements the checks `omarchy-shell` performs on
`manifest.json` so a malformed manifest fails in CI rather than as a blank slot on someone's
bar. `tests/validate_qml_api.py` and `qmllint` gate the QML in CI; `tests/qml_smoke.sh` is the
runtime check, and runs on a real Omarchy box. CI additionally runs every subcommand **signed
out** and asserts each one still answers structured JSON and exits 0, which is the contract the
widget breaks worst on.
