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

The envelope is versioned: `schema_version` is `1`, and a missing value is read as `1`. `Store.ingest` validates `id`, `revision`, `provider`, `title`, `body`, `observed_at`, `obligations` and `url` against the shared Source contract (ai-dlc `contracts/knowledge-record.schema.json`) and rejects any other version. The other fields below are Work Copilot extensions and are never exported.

```json
[
  {
    "schema_version": 1,
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

## Export to a knowledge repository inbox

`work-copilot export --since <timestamp> --to <knowledge repo> --owner <login>` writes captured sources into `<knowledge repo>/inbox/` as Source records (record format version 1). `--to` and `--owner` default to `WORK_COPILOT_KNOWLEDGE_REPO` and `WORK_COPILOT_KNOWLEDGE_OWNER`; `--scope` defaults to `personal`. The path must be an existing local directory without symlinks. Export never commits, pushes or calls an external system.

Each source gives two new files:

- The raw payload (transcript, thread or marked note lines) goes to `inbox/raw/sha256-<hex>.txt`, named by its sha256, so identical payloads are stored once.
- A Source record, `inbox/<id>--<envelope hash>.md`. Its `id` is a stable slug per source, and the source revision is in `revision`. It holds the metadata, `sources[{id, resource, hash}]` pointing at the raw payload, and a short extract as summary.

Inbox items are candidates for 30 days, marked by `stale_after`. Retention and numbering belong to the knowledge repository's workflow. Export never edits or deletes a file it did not just create.

Export is idempotent. De-duplication hashes the whole exported envelope except `revision`, so an envelope already in the inbox is not written again under a new revision. A changed obligation needs a new revision and gives a new record. Deleted (tombstoned) sources and user-entered `local-user` obligations are not exported.

From the selected vault, only lines starting with `>>` are exported. They form one Source per day: the date in the note's file name, else the capture date. Unmarked lines never leave the vault.

### Customer repositories

The destination's `knowledge.toml` decides which Sources it receives, so customer chat never reaches the team repository (KAN-489, ai-dlc ADR 0007). A folder without a regular `knowledge.toml` file receives nothing.

A Source's projects are its own and those its room is bound to in Work Copilot. A customer's room stays the customer's whatever the payload says. Name the Work Copilot project with the customer's ai-dlc slug. Ids are compared in that shape, casefolded, without accents and with other characters as `-`, so `Acme Corp` also matches `acme-corp`.

- A customer repository declares `[repo] boundary = "customer"` and `project`. It receives only Sources bound to that project alone, written with `scope: project`, `boundary: customer` and `project`.
- A team repository must declare its customers in `[[transcripts.customers]]`, or set `customers = []` under `[transcripts]` when it has none. Without that, it receives only `>>` lines from notes bound to, and naming, no project: Work Copilot can't tell a customer project from an internal one, so it leaves the rest out.
- A team repository that lists `[[transcripts.customers]]` receives no Source bound to a customer project. Once any customer is listed, it also skips a Source bound to no project, since an unbound room may be a customer's. Bind the room to a project to export it.
- Marked note lines are your own words. A `>>` line in a note bound to no project is team-visible, unless the note names a customer project. Bind a note about a customer to that project.
- A malformed `[repo]` or customer list, or the retired `[[transcripts.projects]]`, exports nothing.
- Export checks only what it writes. Records already in the team `inbox/` are not checked again.

Run export once per repository: once with `--to` the team checkout, and once per customer checkout. The result lists each skipped Source under `skipped`, by record id and reason, never by title.

## Review queue from a knowledge repository

`work-copilot review-queue --repo <local clone> --github <owner/repo> --me <GitHub login>` puts what waits on you in "Needs you" as `review` obligations, each with its age:

- open records PRs, due 48 hours after opening. A records PR is labelled `records` or changes Markdown under the check roots or `inbox/`. With `--review all` (the default, for a personal repository) every one is listed, since the nightly batch opens them under your account. With `--review assigned` (a team repository) only those that request you or a team you belong to, that you code-own, that you opened, or that a `--batch-author` opened. Drafts count when they change record files, since ai-dlc `propose` opens drafts; the card shows the draft and the clock runs from opening;
- records under the check roots that you own (`--owner`, else `--me`) whose `stale_after` has passed;
- inbox Source records still `raw` after 25 days, five days before they expire.

An item missing from the next run resolves itself, and reopens if it returns. Defaults come from `WORK_COPILOT_KNOWLEDGE_REPO`, `WORK_COPILOT_KNOWLEDGE_GITHUB`, `WORK_COPILOT_GITHUB_LOGIN`, `WORK_COPILOT_KNOWLEDGE_OWNER`, `WORK_COPILOT_KNOWLEDGE_REVIEW` and `WORK_COPILOT_KNOWLEDGE_BATCH_AUTHORS`. Without `--github`, only the local items are collected.

`serve --review-repo <clone>` with the same options refreshes the queue on the collector poll cadence.

The clone is only read. Pull requests are read with `gh api --method GET`, so `gh` holds the credentials. Work Copilot never approves, comments on or merges a PR. The MCP `get_attention` tool shows `kind`, `url`, `opened_at` and `age_hours`.

## Shared MCP server

After installing the project, configure your agent's MCP client to execute the installed `work-copilot mcp` command with the same `--data-dir` used by the dashboard. Consult `work-copilot --help` for flag order. Alternatively invoke:

```text
/path/to/.venv/bin/python -m work_copilot.mcp_server --data-dir /absolute/private/data-directory
```

The server registers eleven tools: five read-only, five local-write, and `capture_note` when a vault is configured.

Read-only tools: `get_attention`, `get_project(project_id)`, `get_source(source_id, offset?, max_chars?)`, `get_reply_context(obligation_id)`, and `search_context(query, project_id?, workstream?, provider?, since?, until?, limit?)` return observed source passages and revision citations. Search is local full-text retrieval, not semantic interpretation.

Explicit local-write tools: `create_project(id, name, aliases?, rooms?)`, `track_obligation(payload)`, `review_obligation(obligation_id, revision, changes)`, `update_project(project_id, revision, changes)` and `set_priorities(ids, revision)` manage local operational records only. `payload` and both `changes` arguments have typed input schemas, so a client can discover their fields; unknown fields are rejected. Start by creating stable projects with explicit aliases and room IDs; one room may belong to multiple projects. No remote discovery or connection happens when creating this metadata. Configure `WORK_COPILOT_VAULT=/absolute/selected/vault` in your MCP client's environment to additionally expose `capture_note(title, body, project_id?, project_ids?, workstream?)`. Without that explicit setting, the capture tool is absent. Optional project/workstream selections write explicit frontmatter, so ordinary pasted text needs no hand-written JSON. Existing pasted frontmatter stays first and retains its structured actions. Capture creates a new Inbox note and returns source/obligation IDs; it never rewrites an original. Read/write SDK annotations reflect these differences.

For “Waiting on Alex for the Atlas transcript; follow up tomorrow,” your agent resolves the actual project ID and date, then submits structured data such as:

```json
{"intent_key":"atlas-transcript","title":"Get Atlas transcript","project_id":"atlas","status":"waiting","waiting_on":"Alex","follow_up_at":"2026-10-27","owner":"me","question_state":"pending"}
```

The service validates fields and persists the local record; it does not pretend to parse free-form natural language. `follow_up_at` is a personal reminder, distinct from a source-backed `due_at` promise. An unresolved person's display name is not recipient identity verification. Reviewing a record can change its local state but cannot approve a draft or authorize an external action. There are no approve, send, edit-original-source or OS-configuration tools. The stdio server has no exposed network port; protect the process account and local database. These tools provide context to your existing authoring workspace; they do not replace it. The implementation pins the SDK v1 compatibility line (`mcp<2`); see the [official v1 SDK documentation](https://py.sdk.modelcontextprotocol.io/v1/).
