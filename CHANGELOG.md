# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
