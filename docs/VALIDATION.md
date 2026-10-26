# MVP validation boundary

Validation performed on Linux with Python 3.12. No workplace account, real Symphony grant, private work message, external send, or paid model call was used by the application tests.

## Executed

- **79 tests passed** in the final local pytest suite (one upstream Starlette/httpx deprecation warning): store transactions/restart/replay, immutable revisions, many-to-many mapping, unknown ownership and conflicts, question deduplication, source-grounded/stale drafts, timezone-aware Today/week, and FTS5.
- Selected-vault originals, edit/delete/restore, aliases as suggestions, safe paths/symlinks, concurrent capture/inbox/export and separate-process export locking.
- HTTP host/origin/session-token boundaries, local onboarding, capture, mapping, search, tracking and restart.
- Actual official MCP SDK **stdio client/server** initialize → tools/list → create/capture/track/review/search → process exit → persisted recall, plus compact packet pagination/source detail.
- Actual local fictional **HTTP OAuth/MCP server**: metadata, public registration, PKCE callback, read discovery/collection, persisted credentials/SQLite restart, expiry/refresh, write-tool refusal and post-reconnect freshness gate. This is protocol validation, not certification of a company deployment.
- Fresh package installation using the dependency constraints, real localhost HTTP startup, project creation, note intake, search, waiting record, template draft, HTML, queue-only without database initialization, drain, and offline doctor after restart.
- JavaScript syntax checks and static safe-rendering/accessibility contracts. Public tree inspected for secrets/private payloads.

Run the reproducible suite:

```sh
.venv/bin/python -m pip install -c constraints.txt -e '.[test]'
.venv/bin/python -m pytest -q
node --check work_copilot/static/app.js
```

## Not verified here

- Browser rendering/click-through screenshots: browser launch was blocked by execution-environment restrictions. Serving HTML and checking JS contracts is not visual QA.
- Actual model-agent end-to-end session: the existing Codex CLI runtime and one supported temporary-state-directory retry both failed **before model or MCP initialization** with a read-only-filesystem app-server error. Neither attempt initialized a model/MCP conversation or created product data; supported temporary runtime state did not overcome the infrastructure error. The SDK integration tests are real but are not a model-agent session.
- Claude Desktop/CLI and Antigravity configuration/lifecycle hooks. No settings were installed into either product.
- Your Symphony endpoint, scopes, registration policy, read-tool annotation/schema, ACL/deletion/history coverage, or corporate proxy/keyring policy.
- macOS/Windows runtime. A PowerShell launcher is provided but was not executed; tzdata is included for portable IANA timezone lookup.

## Your two-session local-agent smoke test

Use only fictional data first. Select a new empty folder and separate data directory, start the app, then configure the local MCP command in your approved client as shown in README. Set `WORK_COPILOT_VAULT` to that same folder if testing capture.

**Session 1**, ask the client to use tools, not merely describe actions:

> Create project `smoke-atlas`, name “Fictional Atlas”, alias “Smoke AT”, room `fictional-room`. Capture a note for that project saying “Decision: inspect staging evidence; production deployment is unverified.” Track that I am waiting on Alex for the fictional transcript, using intent `smoke-transcript` and an explicit follow-up timestamp tomorrow in my timezone. Do not send anything. Return the tool receipts and IDs.

Check the dashboard and original note yourself. Stop the client/session completely (the SQLite data remains). Do not delete the data directory.

**Session 2**, without pasting Session 1 context:

> Use Work Copilot to recall project `smoke-atlas`, search its staging evidence, and find the pending transcript intent. Cite source IDs/revisions and show the existing follow-up instead of creating another task or request. Draft a response only; do not infer deployment or promise a deadline.

Expected: one canonical transcript obligation, retained note/source citation, explicit uncertainty, and no external action. Confirm the UI shows the same IDs and state. If the client invents receipts or cannot call tools, its MCP configuration has not passed this test.

## Deliberately staged capabilities

Raw collection is not automatic semantic extraction. Existing project aliases supply narrow mapping suggestions only. Structured source fields, user reviews, or client-agent tool calls create obligations. Offline reply templates are labeled; high-quality model writing stays in the user's chosen approved agent. Proposed correction learnings are reviewable source context, not accepted global rules. Hooks transport explicit receipts; they do not scrape activity or prove work completed.

## Local-install readiness pass — 2026-10-26

Continued the existing published MVP, without a version bump, release tag, license change, or company-account connection.

- **140 tests passed**, one upstream Starlette/httpx deprecation warning. This supersedes the 79-test baseline for this reviewed readiness tree, not for all future checkouts.
- New `smoke` command runs nine named phases in an isolated temporary workspace: fresh store, many-to-many projects/rooms, source capture/original preservation, waiting/follow-up/search/draft, edit freshness, activity queue, generated wiki, reopen/deduplication, and actual SDK stdio retrieval. It has no model, credential, or network requirement after installation; supplied real data/vault settings are ignored.
- The final package was installed non-editably into a separate fresh virtual environment. Outside the checkout, the nine-phase smoke passed; a real localhost HTTP fresh/restart exercise also passed project editing, capture, search, action/waiting, overdue, pinning, template drafting, original/wiki persistence, and nonmutating doctor checks.
- `doctor` no longer initializes a store or changes directory permissions. Both diagnostic APIs inspect a private temporary database/WAL snapshot; tests preserve original database/WAL/SHM bytes, modes and timestamps, including missing/corrupt paths and custom database filenames.
- Readiness corrections include exact room-to-project routing, revision-checked configuration edits and pinned priorities, distinct action/reminder/deadline/commitment fields, visible overdue work, and removal of completed/waiting items from personal focus.
- MCP machine identities/revisions remain byte-exact. Urgency lists and direct evidence have usable page cursors; tests cover 900 overdue obligations, long source IDs/revisions, and more than eight manually linked sources. Display snippets—not identities—are shortened.
- Initialization is serialized across independent processes, including WAL configuration and legacy schema migration. The regression suite includes 20 concurrency rounds and synchronized independent-process startup. Project rename/search preserves upstream original text while indexing the current canonical name.
- JavaScript syntax, executed pure focus-selection behavior, Python compilation, whitespace checks, and local documentation links were checked. This does not establish rendered-browser usability.

The new command and laptop instructions are in [Getting started](GETTING_STARTED.md). [Client setup](CLIENT_SETUP.md) distinguishes officially documented configuration syntax from an actually executed agent session. The earlier blocked model/browser tests remain blocked/unverified; no retry into restricted paths was performed in this readiness pass.
