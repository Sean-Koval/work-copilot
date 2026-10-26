# Work Copilot

**Your work context, on your laptop.** An evidence-backed attention queue, project wiki, and reply-preparation companion for Claude, Antigravity, and a small localhost dashboard.

This is an early **local-first proof of concept**, not an enterprise-ready service. It runs without a paid model or cloud account. It never sends messages, creates rooms, or changes Jira. The demo contains only fictional work.

## Run it

Requires Python 3.11+ and an internet connection for the first dependency installation.

```sh
git clone https://github.com/Sean-Koval/work-copilot.git
cd work-copilot
./run.sh
```

Windows PowerShell launcher: `./run.ps1` (provided but not runtime-tested). Linux/Python 3.12 is the verified runtime; your laptop OS still needs a smoke test. Open **http://127.0.0.1:8765**. Stop with Ctrl+C. Re-running keeps your demo state. Nothing is installed as a login service.

By default, demo data lives in `~/.local/share/work-copilot-demo` (or `$XDG_DATA_HOME/work-copilot-demo`), **not in this repository**. Real local state uses the separate `work-copilot` directory. Use `--data-dir` **before** the command to choose a different private location; do not choose a public checkout or sync a live SQLite database.

```sh
# Real, initially empty workspace; explicitly select approved notes.
./run.sh serve --vault /absolute/path/to/work-vault

# Explicit custom private storage location.
./run.sh --data-dir /private/work-copilot serve --vault /absolute/path/to/work-vault
```

The selected folder is recursive: choose a dedicated **work-only folder**, not your whole personal vault. The app reads Markdown files, not arbitrary attachments. It does not change handwritten notes. Dropping a Markdown transcript into this folder or pasting one through **Capture note** uses the same pipeline. Unmapped notes appear for review rather than being guessed into a project.

## What works now

- **Today** keeps a suggested daily priority snapshot anchored; newly urgent items appear separately. Suggestions are not promises. **This Week** separates explicit commitments from other work.
- **Attention and waiting** track distinct canonical question intents, evidence, candidate ownership, and pending/sent/answered state. Recording “sent” is a manual record, not a send action.
- **Projects** combine multiple rooms and commercial, engineering, and product workstreams. A shared room/source can belong to multiple projects.
- **Reply preparation** generates clearly labeled offline templates and inspectable source packets. Review/edit/copy locally; no LLM is called. New evidence invalidates old drafts. A merged PR is never treated as proof of deployment.
- **Selected vault capture** detects content edits and deletions, retains source revisions, and preserves daily originals. Structured metadata is optional; this is not general-purpose AI extraction.
- **Readable Markdown wiki** lives in `Work Copilot Generated/` within your selected vault: project projections, source passages/citations, and a review bucket. These generated files are not a second editable source of truth. Use original notes or revision-checked reviews to correct knowledge.
- **Search** uses SQLite FTS5 ranked lexical retrieval with project/workstream/provider/time filters and source-linked snippets. It is not semantic search; no embedding model runs.
- **Follow-ups** are structured local records with a follow-up time separate from a promised deadline. Ask your agent to track a waiting item, then inspect the same record in the UI.
- **One shared context store** is exposed through local HTTP and the official MCP SDK over stdio. Agents remain your writing/coding workspace.
- **OAuth remote MCP** has explicit endpoint/tool configuration and opt-in authorization; see setup below. No account is connected automatically.

A missing meeting transcript is a **waiting-for** item and a draft request—not evidence we have the transcript. Decisions, risks, and awareness remain visible as source context even without an action. Matching explicit intent keys deduplicates repeated transcript/summary actions; arbitrary prose similarity does not.

## First real-work setup

1. Start an empty workspace with `./run.sh --timezone America/New_York serve --vault /absolute/path/to/work-vault` (choose your IANA timezone; default UTC).
2. Use **Create project** in the dashboard to set a stable ID, display name, aliases, and multiple room IDs. A room can belong to more than one project. The local MCP `create_project` tool does the same.
3. Paste/drop a note or transcript; select its project/workstream explicitly, or review its proposed mapping. Agents can call `capture_note` with a project ID—no handwritten JSON required.
4. Configure approved OAuth MCP access separately; no connector is granted automatically.
5. Run offline diagnostics: `./run.sh doctor --vault /absolute/path/to/work-vault --config /private/symphony.json`. It checks local paths/database, project setup, and known sync/auth state; it does not contact or authorize a server.

**How it knows what you did:** this MVP accepts an explicit session handoff as a note, structured obligation update, or normalized import. Include commit/test/source references. A client saying “done” is not evidence of deployment. It does not scrape Claude/Antigravity private trace directories or claim support for their proprietary activity-log formats.

## Give notes explicit context

Supported frontmatter is intentionally small: each field is a single-line JSON value, not full YAML.

```markdown
---
project_ids: ["atlas"]
workstream: "engineering"
obligations: [{"intent_key":"release-checklist", "title":"Confirm release checklist", "project_id":"atlas", "owner":null, "status":"candidate"}]
---
# Release meeting
Decision: run the staging check before considering release.
Risk: production rollout has not been verified.
```

Stable project IDs and explicit intent keys matter; a display name is not an identity. Unknown ownership remains a candidate. Ordinary notes without this metadata are still captured with line-numbered source context and a mapping-review item. Exact whole-phrase project names, IDs, and configured aliases (including wikilinks) propose possible projects; these are narrow heuristic suggestions, never confirmed assignment or inferred ownership. Shared aliases stay ambiguous until reviewed. Rename/move currently behaves as delete plus new source; editing a note in place preserves its source ID.

## Connect your Symphony OAuth MCP server

The connector talks **MCP over HTTP with OAuth**, not Symphony bot APIs. The specific company endpoint, OAuth registration policy, scopes, read tools, response schema, and permitted rooms must be supplied by your administrator or server documentation. We have **not tested against your company server** and do not invent Symphony tool names.

Read [Symphony MCP setup](docs/SYMPHONY_MCP.md) for the exact configuration, credential storage choice, authorization flow, discovery, and normalization contract. User authorization is only initiated when you explicitly run:

```sh
./run.sh connect --config /private/symphony.json --authorize
./run.sh collect --config /private/symphony.json
./run.sh serve --vault /absolute/path/to/work-vault --connector-config /private/symphony.json
```

A configured collector runs while the service is open, without asking Claude to check each time. Closing the dashboard does not stop the running service; closing the service or sleeping the laptop does. Coverage/error/reconnect status is visible. OAuth access is not authorization to send; the collector is read-only and this app has no external send operation.

Jira, GitHub, Confluence, and meeting-provider **vendor-specific adapters are not implemented**. Their fictional demo evidence proves reconciliation behavior, not live connectivity. Normalized JSON import is immediately usable for approved exports or adapters; see [connector contract](docs/CONNECTORS.md).

```sh
./run.sh import /private/approved-source-export.json
./run.sh capture /absolute/path/to/work-vault
./run.sh export /absolute/path/to/work-vault
```

## Use from Claude / Antigravity

After installation, configure an MCP stdio server in the client's supported configuration. Use **absolute paths** and the same data directory as the dashboard:

```json
{
  "mcpServers": {
    "work-copilot": {
      "command": "/absolute/path/to/work-copilot/.venv/bin/python",
      "args": ["-m", "work_copilot", "--data-dir", "/private/work-copilot", "mcp"]
    }
  }
}
```

On Windows, use `.venv/Scripts/python.exe`. Tools provide attention, project context, search, and reply evidence. Explicit structured tools can track obligations and revision-check local corrections; `capture_note` writes only when the server has an explicitly selected `WORK_COPILOT_VAULT` environment variable. Agent credentials cannot approve or send an external action. For example, ask your client: “Track that I am waiting on Alex for the Atlas transcript; follow up tomorrow.” The client must translate that into a stable project ID and an explicit timestamp with timezone; the service validates and persists fields, not natural-language inference. A follow-up reminder is not a promise to Alex, and no request is sent. Client-specific installation UI varies. Approve your company's permitted model/data policy before giving work content to any agent.

## Optional session activity receipts and lessons

[Local hooks/intake](docs/HOOKS.md) accepts small structured session outcomes with repository→project mapping, commit/PR/Jira links, and optional procedure-step references. It has a durable bounded queue, replay deduplication, restart recovery, visible error receipts, and a recursion guard. Nothing is installed into Claude or Antigravity; their lifecycle hook APIs have **not** been verified. No trace directories are scanned.

Outcomes remain **claims**, not verified completion or evidence that a stakeholder was informed. An activity receipt asks for source verification; it does not silently mark deployment, send a reply, or advance a procedure. Correction learnings enter project context as **proposed** review candidates. A full accepted-learning/supersession lifecycle and automatic skill updates are deferred.

MCP defaults are compact: paged attention, bounded evidence snippets, counts/cursors, and a 24,000-character response budget. Use `get_source` for specific passages (up to 8,000 characters per call), instead of loading an entire project history.

## Safety and limits

- HTTP binds only to loopback. Host/origin checks, a session mutation token, and a strict content policy protect against browser cross-site writes. This is a single-user local app, **not remote authentication**; other software running as you can access local state.
- Source text is untrusted data, rendered as text rather than executable HTML. Selected-vault symlinks are excluded. Generated artifacts are never executed.
- SQLite and Markdown contain work content. The app does **not** encrypt the database or implement enterprise retention, backups, or live upstream ACL enforcement. Use company-approved storage and OS disk encryption. Remote access revocation cannot be guaranteed instantaneously; gaps and unsupported semantics must be reviewed.
- Raw OAuth messages/transcripts are collected as source context; the prototype does **not** automatically extract all replies owed or tasks from that prose. Obligations come from explicitly structured source fields, reviewed intake, or your chosen client agent reading context and calling `track_obligation` with evidence IDs. The agent can draft using the same packets, but no always-on LLM runs in this app. The prototype does not resolve aliases semantically, prove source completeness, or automatically reconcile arbitrary contradictory prose. Explicit candidates and review are deliberate.
- No automatic OS permissions, startup agents, cloud deployment, paid model, or GitHub Actions workflow. No license is selected yet; public source availability alone does not grant an open-source license.

Direct runtime dependencies, including the OAuth MCP SDK, are pinned; `constraints.txt` records the tested dependency set. Launchers install on first run, when the dependency manifest changes, or when required imports are absent; unchanged normal launches do not run pip or require network. Review and test dependency updates deliberately.

The original longer-term proposal is preserved in [Architecture](docs/ARCHITECTURE.md); its hosted deployment and external-action design are **future proposals**, not current features.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests cover store replay/restart, source revisions, many-to-many mapping, stale drafts, review conflicts, selected vault preservation/symlinks, local HTTP boundaries, and actual MCP client/server initialization and tool calls. OAuth protocol tests use a local fake server; they do not certify a deployment-specific Symphony server.

See [Validation and local agent smoke test](docs/VALIDATION.md) for what was actually exercised and what remains unverified.
