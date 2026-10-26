# Capture and connector contracts

## What works without a work account

- A selected local Markdown/Obsidian vault is real, read-only file capture. Background scans run only while the service is running. Originals are never rewritten.
- Paste a transcript or note into the dashboard's inbox. This explicitly creates a new private `Inbox/<date>-<random-id>.md` in your selected vault, then captures it. Full context, decisions, risks and awareness remain source passages, even with no extracted action.
- Normalized JSON import ingests evidence through the same SQLite ledger as fixtures and notes.
- The fictional demo simulates multiple Symphony rooms, Jira, GitHub, Confluence and meeting evidence. It is not a verified integration with any of those products.
- An official SDK **local-context stdio MCP server** lets your existing Claude/Antigravity client read the same state. Choose an approved client/model before exposing work context to it. Running locally does not make the client's model local.

## Selected vault metadata

Without metadata a note is captured as evidence plus a **project-mapping review candidate**, not silently assigned to a project or to you. We do not infer ownership from tags or checkboxes. Exact stable IDs are user-supplied; fuzzy aliases are not resolved automatically.

The intentionally small frontmatter grammar recognizes only these fields; JSON arrays are required for lists (this is not a general YAML parser):

```markdown
---
project_ids: ["atlas", "beacon"]
workstream: engineering
obligations: [{"intent_key":"release-confirmation","title":"Confirm staging release","project_id":"atlas","owner":null,"status":"candidate","question_state":"pending"}]
---
# Meeting — fictional example
Decision: validate staging before discussing production.
Risk: deployment evidence has not been provided.
Awareness: the commercial and engineering rooms share this project.
```

`owner: null` stays unresolved. Set `owner: "me"` only with explicit evidence; missing ownership is never guessed. `project_ids` supplies IDs, not display-name matching. A shared note may support multiple projects. Different per-project workstreams can be represented through normalized JSON import.

Each source retains relative note path, content revision, original file link and `L<number>` passage labels. Editing a file keeps its source ID; reverting or restoring it creates a new revision. Renaming a file is represented as old-path deletion plus new-path source (no fuzzy identity merging). Deletions become tombstones; historical snapshots may remain in SQLite. Exports remove deleted current evidence. This is not secure erasure or an organizational retention implementation.

Only UTF-8 `.md` files up to 2 MB are captured. Hidden directories, symlinks and `Work Copilot Generated/` are excluded. An unreadable/invalid note records an error and suppresses deletion reconciliation for that scan. The selected root must already exist and contain no symlink path components. Select only permitted work folders, not an entire personal vault by accident.

## Readable generated project wiki

The app exports only to `Work Copilot Generated/` inside the selected destination. Its ownership manifest protects existing handwritten files from initial takeover. The folder contains an index, review bucket and stable per-project pages with workstreams, obligations, source revisions, passage text and original links. Generated pages are labeled projections: correct source notes or use the dashboard, then regenerate. They are not a second editable task database. Do not edit generated files expecting those changes to be imported. Source HTML is escaped, and generated files are excluded from capture to prevent self-confirming summaries.

## Normalized source envelope

JSON imports are a list of the following objects. A live adapter must map its **verified** schema into these objects; a provider name does not establish API support.

```json
[
  {
    "id": "example:tenant:message:42",
    "revision": "provider-revision-2",
    "provider": "example",
    "title": "Fictional release question",
    "body": "Is staging validation complete?",
    "url": "https://example.invalid/message/42",
    "observed_at": "2026-10-26T12:00:00+00:00",
    "room_id": "example:tenant:room:commercial",
    "room_name": "Commercial",
    "projects": [{"id":"atlas","name":"Atlas","workstream":"commercial"}],
    "obligations": [{"intent_key":"staging-validation","title":"Confirm staging validation","project_id":"atlas","owner":null,"status":"candidate","question_state":"pending","commitment":false}],
    "deleted": false
  }
]
```

Source IDs must include provider + account/tenant + object identity to avoid collisions. Revisions are immutable: replay is idempotent; changed content needs a new revision. Timestamps require timezone offsets. A tombstone retains source ID and uses a new revision with `deleted: true`. Canonical obligation identity is `(project_id, intent_key)`, so two source records can support one question. Producers must choose intent keys deliberately; semantic deduplication is not claimed.

A connector capability/config record should state:

- Transport, verified tool names and normalized read-result schema; no guessed Symphony OBO tool contract.
- Approved accounts, rooms/projects and history window; explicit pagination/cursor behavior and overlap.
- Stable object IDs, edit/delete support, timestamp ordering, checkpoint and rate-limit semantics.
- Authentication expiry/reconnect behavior and access-revocation limitations.
- `read=true`, `external_send=false`; unsupported capabilities stated explicitly.

Commit received records and their checkpoint atomically. Persist gaps/errors; do not claim complete coverage after a failed page. A timeout on a hypothetical external send would be an **unknown outcome**, never a reason to retry blindly. This MVP has no external send executor.

## Shared MCP server

After installing the project, configure your agent's MCP client to execute the installed `work-copilot mcp` command with the same `--data-dir` used by the dashboard. Consult `work-copilot --help` for flag order. Alternatively invoke:

```text
/path/to/.venv/bin/python -m work_copilot.mcp_server --data-dir /absolute/private/data-directory
```

Read-only tools: `get_attention`, `get_project(project_id)`, `get_reply_context(obligation_id)`, and `search_context(query, project_id?, workstream?, provider?, since?, until?, limit?)` return observed source passages and revision citations. Search is local full-text retrieval, not semantic interpretation.

Explicit local-write tools: `create_project(id, name, aliases?, rooms?)`, `track_obligation(payload)` and `review_obligation(obligation_id, revision, changes)` manage local operational records only. Start by creating stable projects with explicit aliases and room IDs; one room may belong to multiple projects. No remote discovery or connection happens when creating this metadata. Configure `WORK_COPILOT_VAULT=/absolute/selected/vault` in your MCP client's environment to additionally expose `capture_note(title, body, project_id?, project_ids?, workstream?)`. Without that explicit setting, the capture tool is absent. Optional project/workstream selections write explicit frontmatter, so ordinary pasted text needs no hand-written JSON. Existing pasted frontmatter stays first and retains its structured actions. Capture creates a new Inbox note and returns source/obligation IDs; it never rewrites an original. Read/write SDK annotations reflect these differences.

For “Waiting on Alex for the Atlas transcript; follow up tomorrow,” your agent resolves the actual project ID and date, then submits structured data such as:

```json
{"intent_key":"atlas-transcript","title":"Get Atlas transcript","project_id":"atlas","status":"waiting","waiting_on":"Alex","follow_up_at":"2026-10-27","owner":"me","question_state":"pending"}
```

The service validates fields and persists the local record; it does not pretend to parse free-form natural language. `follow_up_at` is a personal reminder, distinct from a source-backed `due_at` promise. An unresolved person's display name is not recipient identity verification. Reviewing a record can change its local state but cannot approve a draft or authorize an external action. There are no approve, send, edit-original-source or OS-configuration tools. The stdio server has no exposed network port; protect the process account and local database. These tools provide context to your existing authoring workspace; they do not replace it. The implementation pins the SDK v1 compatibility line (`mcp<2`); see the [official v1 SDK documentation](https://py.sdk.modelcontextprotocol.io/v1/).
