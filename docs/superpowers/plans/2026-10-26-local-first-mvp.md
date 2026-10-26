# Local-first MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task.

**Goal:** runnable, offline-first working proof of evidence-backed attention and replies.
**Architecture:** SQLite service shared by HTTP and stdio MCP; selected notes plus explicit fictional fixtures.
**Tech Stack:** Python 3.11+, FastAPI, uvicorn, MCP SDK, pytest, vanilla JS.
**Spec:** ../specs/2026-10-26-local-first-design.md

## Global Constraints
- No external sending or automatic model calls. No private payloads or credentials in repo.
- Default bind 127.0.0.1, explicit selected vault, private data outside checkout.
- Stable IDs, optimistic revisions, source-grounded text, unresolved candidates stay unresolved.

## Review Focus
- Replayed, edited or deleted evidence cannot silently remain fresh.
- Multiple projects share a room; no inferred user ownership from mention alone.
- Symlinks and script payloads must not escape or execute.
- Draft review does not mean sent or task resolved.
- Restart/overlap must not duplicate intents or reset anchored daily priorities.

### Task 1: Store and reconciliation
Files: work_copilot/store.py, demo.py; tests/test_store.py.
Interface: Store(path, now=None) with snapshot(), project(id), ingest(list[dict], connector='import', checkpoint=None), review(id, revision, changes), draft(id), save_draft(id,revision,text), set_connector(name,status,error=None), seed_demo(store).
- [x] Write failing tests for restart/dedupe/mapping/ambiguous ownership/revision conflict/stale drafts.
- [x] Implement SQLite transactions, normalized sources with explicit projects and obligations; canonical keys; anchored plan.
- [x] Run tests; report interface and evidence.

### Task 2: Vault and MCP
Files: work_copilot/vault.py, mcp_server.py; tests/test_vault.py, test_mcp.py.
Interface: capture_vault(store, root)->dict, export_projects(store, destination)->dict, serve_mcp(data_dir)->None.
- [x] Write failing tests preserving handwritten notes, edit/delete, symlink escape and SDK initialize/list/call.
- [x] Implement selected recursive Markdown read with stable source identity and structured metadata; generated exports separate.
- [x] Implement read-only official SDK tools snapshot/project/draft context; no action approval tool.
- [x] Run tests and report.

### Task 3: Dashboard
Files: work_copilot/static/index.html, app.js, style.css; tests/test_ui_contract.py.
Interface: GET /api/state, GET /api/projects/{id}, POST /api/obligations/{id}/review {revision,changes}, POST /api/obligations/{id}/draft, PATCH /api/drafts/{id} {revision,text}; mutation token GET /api/session returns {token}; X-Copilot-Token header.
- [x] Write contract tests then implement responsive accessible dashboard with Today/week/projects/settings, evidence, revision checked review and copy.
- [x] Ensure all data uses textContent and allow only safe http/https links.
- [x] Verify JavaScript syntax/static contracts; browser rendering attempted but infrastructure-blocked, documented in VALIDATION.md.

### Task 4: Integration and launch
Files: app.py, cli.py, __main__.py, pyproject.toml, run.sh, README.md, tests/test_api.py.
- [x] Write API tests for host/origin/token, optimistic conflict and disabled sends.
- [x] Implement one-command run, periodic selected-vault capture, import/export/demo CLI; documents clearly separate current features from architecture.
- [x] Fresh install, full pytest, live HTTP/browser, actual MCP client, restart and public-content review.
- [x] Parent independent whole-tree review: release-blocking bugs fixed and 79 tests pass; parent retains publication ownership.
