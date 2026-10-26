# Optional local activity hook

This is a bounded **local JSON intake**, not an installed Claude/Antigravity integration. No client lifecycle is assumed or verified. Nothing reads private conversation/session traces, scans a repository, executes code from a payload, installs configuration, or calls a network service.

A caller you explicitly configure may export a small receipt after a turn/session and invoke:

```sh
work-copilot --data-dir /absolute/private/copilot activity --file /absolute/private/event.json --queue-only
work-copilot --data-dir /absolute/private/copilot drain-activity --config /absolute/private/hooks.json
```

Or omit `--queue-only` to ingest immediately. Check `work-copilot --help` for installed command syntax. These are generic script invocations; consult and verify your actual client's documented hook interface before manually wiring one. Do not point this at arbitrary client trace directories. Do not put work payloads or config in this public repository.

## Receipt schema (fictional)

```json
{
  "session_id": "fictional-session-1",
  "turn_id": "turn-4",
  "event_id": "result-1",
  "repo": "example-repo",
  "project_id": "atlas",
  "summary": "Prepared a patch and wrote validation notes",
  "outcomes": ["Tests passed", "Pull request merged"],
  "evidence_urls": ["https://example.invalid/pr/42"],
  "observed_at": "2026-10-26T12:00:00+00:00"
}
```

`session_id`, `turn_id`, `event_id` and `summary` are required (`repo` and `project_id` are optional). `observed_at` is optional; when supplied it requires an ISO timezone. Outcome strings are always **claimed, not independently verified**. A merged PR claim never establishes deployment or resolves another obligation. URLs are recorded as supplied HTTP(S) links, never fetched. A `repo` is a literal label, not permission to access a repository or filesystem path.

An exact optional config map can link known labels:

```json
{
  "repo_projects": {
    "example-repo": {"id":"atlas","name":"Atlas","workstream":"engineering"}
  },
  "authority": {"jira":"high-level outcomes","github":"implementation evidence"},
  "max_events": 100
}
```

Explicit `project_id` wins over an exact repository map. No fuzzy matching, repo discovery or automatic ownership assignment occurs. Unmapped receipts get a project-review candidate. Authority metadata states your intended source roles; it does not verify a claim or override those sources. New activity sets a separate local `refresh_needed` flag; this does not mark a remote connector healthy, fetch anything, or authorize network access.

## Proposed correction learnings

An optional `learning` object carries context worth reviewing:

```json
{
  "scope":"project",
  "project_id":"atlas",
  "correction":"Do not infer deployment from a merged PR",
  "preferred_approach":"Look for deployment evidence or explicitly state the gap",
  "conditions":"Release-status questions",
  "session_ref":"fictional-session-1",
  "supersedes":"optional-prior-reference"
}
```

Allowed scopes are `session`, `project`, `repository`. `correction` and `preferred_approach` are required. This becomes **proposed source context plus a review candidate**, never an accepted/global skill, agent instruction, or harness-rule update. Resolving or dismissing its attention card does not accept the learning. A full versioned accept/reject/supersession learning lifecycle is deferred. The `supersedes` field is only a proposed reference, not an executed change.

## Durability and bounds

Stable `(session_id, turn_id, event_id)` identity and content revision support replay without duplicate sources across restart. The queue uses atomic private JSON files and processes timestamp-ordered filenames. A crash after ingest but before removal replays safely. The oldest failed event stays queued with a `.error.json` receipt; draining stops rather than skipping it silently. Review/correct or remove that event yourself, then drain again. No auto-retry loop or daemon is installed.

Limits: 64 KiB events, 16,000-character summaries, 30 outcome strings, 30 evidence URLs, 1,000 queued events, and at most 100 events per drain by default (configurable 1–1,000). Queue-only is the lightweight path; no SQLite initialization is required by the enqueue function. `WORK_COPILOT_HOOK_ACTIVE=1` skips enqueue/ingest/drain to help a caller prevent recursive hook invocation. The hook itself never launches another client or hook. Keep the data directory on local disk, not a shared live synchronization folder. FIFO means filename timestamp order; concurrent callers and system clock changes are not a transactional global ordering guarantee.

## Procedure references, not a workflow engine

Optional `procedure_id`, `procedure_version`, and `step_id` are literal metadata references. They are recorded as **not executed or advanced**. Keep reusable procedures/skills in their source-controlled authoring location. Project stage/state requires current source evidence. Correction learnings remain proposed context until a future explicit learning-review lifecycle exists. This hook neither duplicates runbooks nor runs a DAG, advances stages, marks stakeholders informed, or edits skills.

An OS file lock permits only one drain at a time; another returns `busy: true` without touching events. The queued receipt is removed only after both source ingestion and the separate local refresh flag succeed. A simulated interruption between those operations is covered by a replay test: the source deduplicates and the flag is reasserted before removal. No network work is triggered by that repair.
