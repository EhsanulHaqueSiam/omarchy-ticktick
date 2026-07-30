# omarchy-ticktick

[![CI](https://github.com/EhsanulHaqueSiam/omarchy-ticktick/actions/workflows/ci.yml/badge.svg)](https://github.com/EhsanulHaqueSiam/omarchy-ticktick/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Omarchy](https://img.shields.io/badge/Omarchy-shell%20plugin-8aadf4)](https://omarchy.org/)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://www.python.org/)

**TickTick in your Omarchy bar.** Due counts at a glance, a keyboard-driven popup to
browse, tick and edit tasks, and natural-language quick add — without leaving the bar.

Edits land **instantly and locally**, then sync. Close the lid mid-flight, tick things
off on a train with no signal, and it all arrives when you reconnect.

```
 ☑ 3                       ← overdue + due today, urgent-coloured when you're behind
 ┌─────────────────────────────────────┐
 │  ☑  TickTick                        │
 │     1 overdue · 2 today             │
 │  [Today] Next  All  Projects        │
 │  ⌕ search…                          │
 │  + tomorrow 5pm !high #work         │
 │                                     │
 │  OVERDUE                            │
 │  ☐ Renew domain      Personal  ●  Tue│
 │  TODAY                              │
 │  ☐ Submit report     Work      ●  17:00│
 │  ☐ Pack for trip     Home  1/3 ○  today│
 └─────────────────────────────────────┘
```

## Install

```bash
omarchy plugin add https://github.com/EhsanulHaqueSiam/omarchy-ticktick.git --enable
```

That is a `git clone` into `~/.config/omarchy/plugins/siam.ticktick/` and nothing else —
no build step, no `pip install`, no daemon, no Node. The helper is standard-library
Python 3.11+, which Omarchy already has.

Then sign in: click the widget, paste a token. That is the whole setup.

<details>
<summary>Placing and updating the widget</summary>

```bash
omarchy bar plugin move siam.ticktick center   # left | center | right
omarchy plugin update siam.ticktick            # shows a diff, then fast-forwards
omarchy plugin remove siam.ticktick
```
</details>

## Signing in

Open TickTick on the web, click your avatar → **Settings → Account → API Token**,
create one, and paste it into the widget.

That is it. No app registration, no client secret, no redirect URI to match
character-for-character, no free port on localhost.

The token is passed to the helper over **stdin**, never on the command line, because
anything in `argv` is readable by every process on the machine through
`/proc/<pid>/cmdline`. It is stored at `~/.config/ticktick/credentials.json`, mode
`0600`. Nothing is sent anywhere except `api.ticktick.com`.

From a terminal, if you prefer:

```bash
ticktick login          # prompts, reads the token from stdin
ticktick status
```

**Already signed in to [TickTick's own CLI](https://www.npmjs.com/package/@ticktick/ticktick-cli)?**
Then you are already signed in here — its token is picked up automatically from
`~/.config/ticktick-cli/config.json`. That file is only ever read, never written, and
`ticktick logout` stops the borrowing for good.

<details>
<summary>Browser OAuth instead (the old way)</summary>

Still supported for anyone who would rather not handle a token directly. It needs an
app registered at the [TickTick Developer Center](https://developer.ticktick.com/manage)
with the redirect URL set to exactly `http://localhost:8080/callback`.

```bash
ticktick auth --client-id ID --client-secret SECRET
ticktick auth --redirect http://localhost:9000/callback   # a different port
```
</details>

<details>
<summary>Using dida365.com (滴答清单)?</summary>

It is a separate service with separate accounts and the same API shape. Point the
helper at it once, when you sign in:

```bash
ticktick login --api-base https://api.dida365.com/open/v1
```

`TICKTICK_API_BASE` in the environment overrides it for a single command.
</details>

## Using it

### Mouse

| Where | Action |
|---|---|
| Bar button | left = popup · right = refresh now · middle = jump to quick-add |
| Task row | click the checkbox = complete · click the title = expand detail |
| Expanded detail | click a checklist item to tick it · pick a project to move it |

### Keyboard

The popup is fully keyboard-driven — the mouse is optional.

| Key | Action |
|---|---|
| <kbd>↑</kbd> <kbd>↓</kbd> / <kbd>k</kbd> <kbd>j</kbd> | move the cursor |
| <kbd>Enter</kbd> | complete the task under the cursor |
| <kbd>Space</kbd> | expand / collapse its detail |
| <kbd>←</kbd> <kbd>→</kbd> / <kbd>Tab</kbd> | switch view |
| <kbd>1</kbd>–<kbd>4</kbd> | jump to Today / Next / All / Projects |
| <kbd>a</kbd> | quick-add |
| <kbd>/</kbd> | search |
| <kbd>p</kbd> | cycle priority |
| <kbd>x</kbd> / <kbd>d</kbd> | delete (with confirmation) |
| <kbd>r</kbd> | refresh |
| <kbd>Esc</kbd> | back out one layer, then close |

### Natural-language quick add

Type a whole task in one line. Anything it does not recognise stays in the title
verbatim, so `call #1 supplier` is left alone rather than mangled.

```
submit report tomorrow 5pm !high #work
pay rent on the 1st !medium *monthly
dentist next tuesday 9:30am
review PR in 3 days
```

| Token | Meaning |
|---|---|
| `today` `tomorrow` `tonight` `mon`…`sun` `next monday` `in 3 days` `eod` `eow` `2026-08-14` `14 aug` | due date |
| `5pm` `5:30pm` `17:30` `@9am` `noon` `midnight` | time of day (without one, the task is all-day) |
| `!high` `!medium` `!low` `!none` (`!p1`…`!p0`) | priority |
| `#work` `#"two words"` | project (falls back to your default if unknown) |
| `*daily` `*weekly` `*monthly` `*weekdays` `*every 3 days` | repeat rule |

Preview the parse without creating anything:

```bash
ticktick parse "submit report tomorrow 5pm !high #work"
```

## Works offline

Every read is answered from a local cache, and every write is applied locally first,
queued, and sent when it can be. Tick a task off with no network and it leaves the
list immediately; the queue survives reboots and drains on its own.

```bash
ticktick complete <taskId> --offline   # apply now, send later
ticktick sync                          # flush the queue and refresh
ticktick status                        # "3 changes waiting to sync"
```

The cache lives at `~/.local/state/ticktick/state.json`, mode `0600`. Signing out
deletes it, because it holds your task titles.

Two consequences worth knowing:

- A task created offline gets a temporary id until it syncs, at which point it adopts
  the real one. Edits you made to it in the meantime follow it.
- If the server refuses a queued write on its own terms — the task was deleted from
  another device, say — that entry is dropped and the rest of the queue continues. A
  server *outage* is different: those stay queued and retry.

## The CLI

The widget is a thin shell over a real command-line tool. Everything the popup can do
you can do from a terminal, which is also what makes the whole thing debuggable.

It prints a formatted view to a terminal and **exactly one JSON object** to a pipe, so
the same command serves a human and the widget without a flag to remember.

It lives inside the plugin. Put it on your `PATH` once:

```bash
mkdir -p ~/.local/bin
ln -sf ~/.config/omarchy/plugins/siam.ticktick/bin/ticktick ~/.local/bin/ticktick
```

```bash
ticktick status                              # signed in? counts, pending writes
ticktick tasks --view next --days 14
ticktick tasks --tag errand --priority high
ticktick search invoice --all-states         # searches completed tasks too
ticktick add "submit report tomorrow 5pm !high #work"
ticktick add "call plumber" --tag urgent --start today
ticktick complete <taskId>
ticktick edit <taskId> --priority 3 --due "next monday 9am"
ticktick edit <taskId> --add-tag errand --remove-tag urgent
ticktick move <taskId> <toProjectId>

ticktick item add <taskId> "pack passport"   # checklists
ticktick item check <taskId> <itemId>
ticktick item rename <taskId> <itemId> "new title"
ticktick item move <taskId> <itemId> 0
ticktick item remove <taskId> <itemId>

ticktick completed --days 7
ticktick projects
ticktick project add "Reading" --color "#F18181"
ticktick project groups
ticktick tags
```

Add `--json` to force machine output on a terminal, `--text` to force the formatted
view through a pipe.

Every JSON-emitting subcommand exits 0 — including on failure
(`{"ok": false, "error": "auth", "message": "…"}`). That contract is what lets the QML
side parse it without ever seeing a traceback.

The shell also exposes IPC:

```bash
omarchy-shell siam.ticktick refresh
omarchy-shell siam.ticktick add "Buy milk tomorrow"
omarchy-shell siam.ticktick toggle
omarchy-shell siam.ticktick count
omarchy-shell siam.ticktick view next
```

## Settings

Configure through the Omarchy bar settings UI, or from a shell:

```bash
omarchy bar plugin set siam.ticktick refreshIntervalSec 120
omarchy bar plugin set siam.ticktick badgeMode Overdue
omarchy bar plugin set siam.ticktick hideWhenEmpty true --json
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `refreshIntervalSec` | 30–86400 | `300` | poll cadence; actions refresh immediately regardless |
| `upcomingDays` | 0–30 | `7` | how far the **Next** view reaches |
| `badgeMode` | Today / Overdue / All / None | `Today` | what the bar number counts |
| `defaultView` | Today / Next / All / Projects | `Today` | which view the popup opens on |
| `defaultProject` | project id | `""` | where quick-add files tasks (blank = Inbox) |
| `hideWhenEmpty` | bool | `false` | remove the widget from the bar when the badge is 0 |
| `showProjectChips` | bool | `true` | show each task's project on its row |
| `includeUndated` | bool | `false` | include tasks with no due date in **All** |
| `confirmDelete` | bool | `true` | confirm before deleting (deletion is permanent) |

## How it works

```
 bar  ─ qml/BarWidget.qml ─ qml/Service.qml ─┐
                                             │  Process + one JSON object per call
                                  bin/ticktick
                                             │
                      ticktick/{cli,store,views,api,…}.py
                                             │  cache + outbox on disk
                                             │  HTTPS
                                  api.ticktick.com/open/v1
```

QML never speaks HTTP — not even for signing in. Every bit of logic worth testing —
date bucketing, the language parser, grouping, sorting, the sync queue — lives in
Python behind `unittest`, and QML is left with presentation only. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## TickTick API notes

Several behaviours were verified by executing against a live account, because the
published documentation is wrong or silent about them. If you are building your own
TickTick client, these will save you an afternoon:

- **The Open API is much bigger than its docs.** `GET /tag`, `GET /project/group`,
  `POST /task/search`, `GET /project/{id}/column`, `GET /habit` and `GET /countdown`
  all work and none are documented. Tasks carry `tags`, `parentId`/`childIds`,
  `sortOrder`, `startDate` and `progress`.
- **`POST /task/search` ignores half its own parameters.** `keywords` and `status` are
  honoured; `projectIds`, `dueFrom` and `dueTo` are accepted and have no effect. It
  does reach completed tasks, which `/task/filter` cannot.
- **Responses carry milliseconds** (`2026-07-31T18:00:00.000+0000`), which does not
  match the documented `yyyy-MM-dd'T'HH:mm:ssZ`. Parsing with `strptime` and that
  format raises on every date — and a parser that swallows the error renders every
  task as undated, silently.
- **All-day due dates are midnight in the *task's own* timezone.** Reading the UTC
  date shifts every all-day task by a day for anyone far enough from UTC.
- **A bare date string is silently discarded.** `{"dueDate": "2026-08-06"}` returns
  `200 OK` and the task comes back with `dueDate: null`.
- **`POST /task/filter` returns every task in one request**, Inbox included. The widely
  repeated "TickTick has no all-tasks endpoint" is out of date, and per-project fan-out
  is exactly what trips the rate limiter.
- **Rate limiting arrives as HTTP 500** with `exceed_query_limit` in the body, not a 429.
- **The Inbox is absent from `GET /project`** but reachable at `GET /project/inbox/data`.
- **There is no PATCH.** Every task update replaces the whole task, so any client must
  send back the fields it did not mean to change — including the entire checklist.

### Limitations

These are TickTick's, not the plugin's:

- A completed task **cannot be un-completed** — no endpoint exists. Undo it in the app.
- No webhooks, so the widget polls.
- Attachments and sharing are not exposed.

Habits, focus/pomodoro records and countdowns do have endpoints, but they are not a
to-do list and this widget deliberately leaves them alone.

## Troubleshooting

**The widget shows nothing / the slot is blank.** That is a QML load error. Check the
plugin is discovered and enabled:

```bash
omarchy plugin list | grep ticktick
omarchy plugin rescan
```

**The count is 0 but I have tasks.** Ask the helper directly:

```bash
ticktick tasks --view all --refresh
```

Undated tasks are never counted — the widget is a due-date view by design. Turn on
`includeUndated` to see them in **All**.

**Changes are not appearing on my phone.** Check the queue:

```bash
ticktick sync
```

If it reports a rate limit, that is TickTick throttling you; the backoff is remembered
across runs and it will drain on its own.

**"Not signed in" after it worked.** Generate a fresh token in TickTick under
Settings → Account → API Token and paste it in again.

**Two TickTick widgets, one stops responding to `omarchy-shell`.** An IPC target
accepts one handler; the first to register wins. Remove the other plugin, or use the
bar button.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Standard library only, and logic that can be
tested belongs in Python rather than QML.

```bash
python -m unittest discover -s tests   # 165 tests, no network
python tests/validate_manifest.py
python tests/validate_qml_api.py
./tests/qml_smoke.sh                   # needs a live Omarchy session
```

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by TickTick or Appest Inc.
