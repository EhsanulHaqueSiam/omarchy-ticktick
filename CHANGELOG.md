# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

TickTick's own rules, in the bar.

### Added

- **The full set of smart lists** — Today, Tomorrow, Next 7, Inbox, All, Lists and Done, on
  <kbd>1</kbd>–<kbd>7</kbd> and in the tab strip. Today is what is due today *plus* whatever is
  late, Tomorrow is tomorrow alone, and a list is not a date filter at all, so the Inbox shows
  its undated tasks the way TickTick does.
- **Sections are your lists.** Rows group under the list they belong to, which is TickTick's
  default for a smart list. <kbd>o</kbd> cycles to grouping by date, priority or title —
  `--sort list|time|priority|title` on the CLI — and the headings follow the order.
- **Folders.** Lists nest under their folder in the Lists tab, read from the `groupId` that
  `/project` already sends. The folder *names* come from an undocumented endpoint, so losing it
  costs the headings and nothing else.
- **Notes are not tasks.** A note (`kind: NOTE`) renders with a ▤ glyph, has no checkbox, cannot
  be completed, and is excluded from the bar badge, the bucket counts and every list count.
- **Filtering** by priority and tag from the popup, on <kbd>f</kbd>.
- **A sync key.** <kbd>s</kbd> pushes anything queued and refetches now. <kbd>r</kbd> stays
  cache-first, which is what makes it instant.
- **Browser sign-in leads.** "Sign in with browser" is the first button in the signed-out
  panel and runs the OAuth flow in a terminal of its own; the API-token field is folded
  away behind it as the fallback. `ticktick auth` now asks for the Client ID it needs
  rather than failing with instructions to carry back to a shell, and remembers it.
- **A new task appears on the keystroke.** It is written to the local cache before the
  helper is even launched, instead of waiting on two process launches and a round trip.
  A task typed inside Today takes today's date and one typed inside a list joins that
  list, so it appears where it was typed — `add --due-default WHEN` on the CLI.
- Real screenshots in the README, rendered from a live Quickshell against the fake
  account by `tests/screenshots.sh`, so they cannot drift from the code.
- **Full task editing in the detail pane** — due date, start date, tags, repeat, reminder, list,
  and adding or removing subtasks, all without leaving the keyboard. Dates take the same natural
  language quick-add does.
- `ticktick edit --remind / --clear-remind / --repeat / --clear-repeat`, so everything the
  detail pane can change is reachable from a terminal too.

### Fixed

- **The checkbox no longer ticks itself under the cursor.** The box previewed what
  <kbd>Enter</kbd> would do by filling in, which is indistinguishable from a task that is
  actually finished — every row you pointed at read as already done. The box now says what the
  task *is*; the cursor sharpens the empty box instead of filling it.
- **Finished work looks finished.** A completed task renders ticked, dimmed and struck through
  the way a ticked subtask always has, dated by when it was *completed* rather than when it was
  due. Completing one again is refused rather than dropping the row and reporting a change that
  never happened — the Open API cannot reopen a task or even look one up once it is closed.
- **The row stops jumping when the pointer crosses it.** The delete ✕ was revealed by width, and
  a `Row` drops an invisible child's width *and* its spacing, so the due date, the priority dot
  and the list chip all slid sideways under a pointer that was aiming at one of them. The ✕ now
  holds its slot and fades, and it burns urgent only under its own pointer instead of on every
  row you happen to hover.
- **The list name is not printed twice.** Under the default sort the section heading above a row
  already *is* its list, so the chip repeated it — `INBOX` over a row chipped `Inbox`. The chip
  now shows only where it says something new: not under list headings, not inside the Inbox, and
  not inside a single list.
- **Every shortcut in the hint line is readable.** Ten of them do not fit across the popup on one
  line, and eliding hid everything after `f filter` behind an ellipsis. It wraps.
- **Notes and single-list views no longer distort the badge.** Counts are computed over the
  whole account regardless of the view, so browsing into a list stops changing what the number
  means. Every view is still exactly one request.
- **Dates no longer drift across a DST change.** `dates.now()` carried a *frozen* UTC offset —
  today's — and due dates were converted through it, so every timed task past the next
  transition rendered an hour out and anything near local midnight landed in the wrong day
  group, for weeks either side, twice a year.
- **A queued write is never silently thrown away.** A captive portal or proxy login page answers
  200 with HTML, which arrived as a verdict-less error and burned an attempt budget; eight polls
  destroyed the queued mutation. Attempts are spent by activity, not by time, so the cap is gone
  and the queue is bounded by age instead.
- **A write the server refuses is reported.** Dropping a rejected entry set no error, so `add`
  answered `ok: true` with an empty warning for a task that will never exist.
- **Work the server already accepted is not sent twice.** The flush at the top of a command was
  only persisted if the rest of the command succeeded, so a later failure re-queued creates that
  had already landed — and creates are not idempotent.
- **A queued edit no longer reverts fields it never touched.** TickTick has no PATCH, so the
  body replaces the task; a retry now re-reads the task first rather than shipping the snapshot
  frozen when the command ran.
- **A subtask created offline finds its parent.** The id remapper walked three named keys, so a
  `local-` `parentId` inside a create body was uploaded verbatim as an id no client can resolve.
- **A create lands in the cache even when the cache was empty**, instead of being appended to a
  throwaway list — which left the following queued operation addressing a task with no project.
- **The popup no longer goes permanently deaf** after moving a task between lists. Committing
  the change destroyed the delegate that owned the "a dropdown has the keyboard" flag before it
  could clear it, and nothing else ever did.
- **A second sign-in attempt actually sends the token.** stdin was disabled imperatively after
  the first attempt and never re-enabled, so every retry failed with a token that never left the
  widget.
- **Multi-monitor bars stay in step.** A completion on one screen now fans out when the write
  settles, rather than leaving every other screen counting it for up to five minutes.
- **Signing in over a borrowed token works.** The marker that keeps another TickTick
  tool's token from being written down here also discarded a token the user had just
  signed in with, so `login` and `auth` reported success and changed nothing.
- **A lapsed token no longer throws the write away.** A mutation made while signed in
  but expired is now applied locally and queued — like being offline — instead of being
  abandoned before it was ever written down. Never having signed in is still an error,
  because there is no account for a queue to drain into.
- **An entry another process queued is not sent twice.** `save` merges in work it did
  not know about, but never recorded having seen it, so the next save merged it back in
  after it had synced.
- **An empty Inbox shows nothing**, rather than the user's entire account under the
  heading "Inbox" — an unknown Inbox id was being read as "no filter".
- **A rate-limit backoff is really capped.** The 5-minute ceiling was applied to the
  number reported, not to the stored stamp the write gate reads, so a clock jump could
  freeze every write for as long as the file claimed.
- **The detail pane no longer renders blank checklists and reminders.** A nested array
  reaches a row delegate as an array-*like* that fails `Array.isArray`, which silently
  discarded every checklist item and every reminder in the expanded pane.
- **Editing in the detail pane no longer kills the keyboard.** Leaving a field left the
  window with no focused item, so the popup stopped answering keys until reopened.
- The Done view sections by date rather than by list, because its rows are ordered by
  when they were finished.
- Two lists the catalogue does not know are no longer interleaved into one heading.
- Deleting a note no longer decrements counts that never included it.
- A task still being saved cannot be completed, deleted or edited through its
  placeholder id — and a write the server refused now says so instead of reporting
  success.
- Hovering a partially-visible row no longer scrolls the list out from under the mouse.
- Escaping out of a list no longer leaves the cursor past the end of the shorter list.
- `ticktick auth` with a closed stdin prints its usage error instead of a traceback.

### Testing

- `tests/model.test.js` — the QML presentation helpers under `node --test`, with no
  packages and no compositor, so CI covers them for the first time.
- `tests/screenshots.sh` renders the README's images from a live Quickshell against the
  fake account, so they cannot drift from the code.

## [1.0.0] — 2026-07-30

First release.

### Added

- **Bar widget** — overdue and due-today counts with an urgent-coloured badge, a tooltip
  summary, vertical-bar support, and an optional hide-when-empty mode.
- **Keyboard-driven popup** — Today / Next / All / Projects views, search, grouped task list
  (Overdue → Today → Tomorrow → Upcoming → Later), and an expandable detail pane per task.
- **Natural-language quick add** — `submit report tomorrow 5pm !high #work *weekly` parses the
  due date, time, priority, project and repeat rule out of the title, conservatively enough that
  ordinary text like `call #1 supplier` is left alone.
- **Local-first reads and writes.** Reads are served from an on-disk cache of raw API payloads
  at `$XDG_STATE_HOME/ticktick/state.json` (0600, written atomically) and refreshed in the
  background, so a poll with no network degrades to the last known-good list instead of blanking
  the widget. Writes apply to the cache immediately, append to a durable outbox, then try to
  flush; anything that cannot be sent is retried on the next call, with no daemon involved. A
  task created offline gets a `local-` id that is remapped to the server's id once the create
  lands. Raw payloads rather than rendered rows are cached, because a row's "today" bucket is
  computed at fetch time and would be wrong by morning.
- **Task actions** — complete, delete (with confirmation), cycle priority, move between
  projects, edit title/content/due/start/priority, attach or clear tags, re-parent a task under
  another, and add, rename, remove, reorder or tick checklist items. Every action the widget
  performs goes through the outbox, so all of them work with no network.
- **Helper CLI** covering task create/read/update/complete/delete/move, completed-task listing,
  `/task/filter`, account-wide search that reaches completed tasks, tags, project groups,
  project create/rename/delete, and an explicit `sync`. `--offline`, `--refresh` and `--max-age`
  control how a command treats the cache.
- **Sign-in with a personal API token** (TickTick web app → Settings → Account → API Token),
  pasted straight into the widget. The token is read over the helper's stdin and never appears
  in argv, which is world-readable through `/proc/<pid>/cmdline`, and it is verified with one
  request before anything is stored at `~/.config/ticktick/credentials.json` (0600). The OAuth2
  browser flow — local callback server, `state` verification — remains as a secondary path. When
  TickTick's own `@ticktick/ticktick-cli` is already signed in on the machine, its token is
  borrowed rather than asking again; that file is only ever read, and `logout` stops the
  borrowing so signing out stays signed out.
- **Human-readable terminal output.** The CLI renders the same payload as formatted text when
  stdout is a terminal and as JSON when it is a pipe, so the widget cannot accidentally receive
  prose; `--json` and `--text` force either shape.
- **Nine settings** exposed through the Omarchy settings UI: refresh interval, upcoming window,
  badge mode, default view, quick-add project, hide-when-empty, project chips, undated tasks,
  and delete confirmation.
- CI on Python 3.11–3.13 covering 165 unit and end-to-end tests (the real CLI over real HTTP
  against a fake TickTick server), the plugin manifest schema, QML syntax, and a check that
  every Omarchy singleton member the QML touches exists. `tests/qml_smoke.sh` loads the widget
  in a real Quickshell and needs a live Wayland session, so it runs on the box rather than in CI.

### Notes on TickTick's API

Several behaviours were verified against the live API because the published documentation is
wrong or silent about them. They are documented in `docs/ARCHITECTURE.md`:

- Timestamps come back with milliseconds (`2026-07-31T18:00:00.000+0000`), which does **not**
  match the documented `yyyy-MM-dd'T'HH:mm:ssZ`. Parsing with `strptime` and that format fails
  on every date and silently renders every task as undated.
- All-day due dates are midnight in the **task's own** timezone, not the viewer's — reading the
  UTC date shifts every all-day task by a day for anyone far enough from UTC.
- A bare date string (`"2026-08-06"`) is accepted with `200 OK` and then silently discarded.
- `POST /task/filter` returns every task in one request, so no per-project fan-out is needed.
  Fanning out is what trips rate limiting, which arrives as HTTP **500** with
  `exceed_query_limit` in the body rather than a 429. The resulting backoff is persisted, since
  every CLI call is a new process.
- The Inbox is absent from `GET /project` but reachable at `GET /project/inbox/data`.
- The API is wider than its documentation: `GET /tag`, `GET /project/group`,
  `POST /task/search`, `GET /project/{id}/column`, `GET /habit` and `GET /countdown` all answer
  200, and task objects carry `tags`, `parentId`/`childIds`, `sortOrder`, `startDate`,
  `progress`, `columnId`, `etag` and `kind`, none of which appear in the documented task schema.
- `POST /task/search` reaches completed tasks, which `/task/filter` and `/project/{id}/data`
  cannot.

### Known limitations

These are TickTick API limitations, not plugin bugs:

- A completed task cannot be un-completed — there is no endpoint. Undo it in the TickTick app.
- `POST /task/search` honours `keywords` and `status` only. `projectIds`, `dueFrom` and `dueTo`
  are accepted and then ignored, returning the same rows with and without them.
- Completed tasks are invisible to `/project/{id}/data` and `/task/filter`; they come from
  `POST /task/completed` or a search.
- There is no per-item endpoint for checklists, so every item edit rewrites the whole task.
- No webhooks, so the widget polls.

[1.0.0]: https://github.com/EhsanulHaqueSiam/omarchy-ticktick/releases/tag/v1.0.0
