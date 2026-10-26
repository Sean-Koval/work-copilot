# Local readiness: evidence before “LOCAL1.0”

**LOCAL1.0 is an acceptance target, not a released version or a certification.** The current repository is a working local-first proof of concept. Do not infer a version tag, license, enterprise approval, or verified company integration from this checklist.

The aim is modest: one person can install it on their laptop, understand the UI, preserve selected notes, recover context across restarts/agent sessions, and prepare evidence-backed replies without accidental external actions.

## What the recorded validation establishes

The [validation record](VALIDATION.md) preserves the original **79-test MVP run** and the subsequent **140-test local-readiness run on Linux/Python 3.12**, plus fresh-install and local HTTP restart checks. These are recorded executions, not a promise that every later checkout or laptop has been verified.

| Area | Evidence recorded | What it does not establish |
| --- | --- | --- |
| Store and reconciliation | Restart/replay, source revisions, mapping, intent deduplication, stale drafts, review conflicts, timezone-aware views, lexical search | Complete understanding of arbitrary work prose |
| Selected vault | Original preservation, edits/deletions/restores, alias suggestions, safe paths and concurrent capture/export | Every OS/filesystem behavior, enterprise retention or backup |
| Local HTTP | Host/origin/token boundaries, onboarding/capture/search/tracking smoke paths | Successful visual rendering or browser click-through |
| Local MCP | Actual official SDK stdio client/server initialization and tool operations, restart recall | A successful Claude/Antigravity/model conversation |
| Remote OAuth MCP | A local fictional HTTP server exercised metadata, registration, PKCE, collection, refresh and safety gates | Compatibility with your company's endpoint, scopes, tools, data schema, proxy, or ACL semantics |
| Packaging | Fresh constrained installation and localhost startup on Linux | macOS/Windows installation; every subsequent dependency update |

Browser launch and model-agent runtime attempts were blocked by the execution environment. Static JavaScript/HTML checks are useful but **not substitutes** for visual QA or a real agent session. No screenshots are claimed.

## Acceptance gates for your laptop

Use fictional content and an isolated data directory first. Record the OS, Python version, commit, command, observed result, and any blocker. A failed or unexecuted gate remains open.

### 1. Install and restart

- [ ] Follow [Getting started](GETTING_STARTED.md) on the target OS without undocumented setup.
- [ ] Run `./run.sh smoke` and inspect its phase results. It uses temporary fictional data, not your real workspace, chosen agent, or company server.
- [ ] Start the dashboard, stop with Ctrl+C, and restart against the same private data directory.
- [ ] Existing fictional projects/notes/obligations remain present without duplicates.
- [ ] An unchanged normal launch does not require a new package installation.
- [ ] `doctor` reports the intended paths, project setup, and known local collection state. Review warnings rather than equating a completed command with production readiness.

### 2. Understand and use the dashboard

- [ ] In an empty real workspace, create a project and capture a note without writing frontmatter.
- [ ] Review an ambiguous mapping; link one source to multiple projects and workstreams.
- [ ] Inspect Today, This Week, waiting, search filters, evidence, and source coverage in your actual browser.
- [ ] Prepare, edit, save, and copy a template; confirm nothing is sent and question state does not silently become sent/answered.
- [ ] Change supporting evidence and confirm stale content cannot be copied as if current.
- [ ] Verify usable keyboard navigation and the screen size you will actually use. Save screenshots only after real rendering, not as fabricated proof.

### 3. Preserve the knowledge you own

- [ ] Select a dedicated approved Markdown folder; preserve existing originals byte-for-byte during capture/export.
- [ ] Edit an original in place and inspect the new captured revision. Test deletion/restoration with disposable notes.
- [ ] Open `Work Copilot Generated/` in Obsidian or a Markdown viewer; inspect project views and cited source passages.
- [ ] Confirm generated exports are separate from editable originals and private application state stays outside the public repository.

### 4. Prove shared context with your actual agent

- [ ] Configure the supported client against the same local data directory and explicitly selected vault.
- [ ] Run both sessions in the [agent smoke test](VALIDATION.md#your-two-session-local-agent-smoke-test), fully ending Session 1 before Session 2.
- [ ] Inspect actual tool receipts and IDs. Confirm persisted recall, one canonical request, evidence citations, and no fabricated deployment or deadline.
- [ ] Confirm your organization's model/data policy permits this use. SDK tests alone do not pass this gate.

### 5. Prove any requested company connector separately

This gate is needed before relying on a live connector, not to try the offline demo or local-vault workflow.

- [ ] Obtain the approved endpoint, OAuth policy/scopes, exact read tools, response contract, and selected room/project scope.
- [ ] Verify authorization, collection, expired-auth recovery, and catch-up against that server with permitted test data.
- [ ] Inspect source identity, revisions, pagination, edit/delete behavior, coverage gaps, and access-revocation limitations.
- [ ] Confirm no unsupported vendor capability is implied. Jira/GitHub/Confluence/meeting-provider adapters remain unimplemented unless an approved normalized adapter is supplied.

## Stopping condition

Call the **local workflow accepted for a named setup** only when the applicable gates have real evidence and no unresolved blocking failures. Keep workplace-connector acceptance separate from offline acceptance. Document deferred gates explicitly; do not label the whole product “1.0” because the unit tests pass.

Still outside this target: always-on capture while a laptop sleeps, automatic semantic reconciliation of all work messages, enterprise multi-user access/retention, guaranteed upstream ACL enforcement, automatic external sending, and production cloud deployment.

For maintainers, rerun the documented suite and installation smoke checks for the exact checkout being evaluated. Keep [VALIDATION.md](VALIDATION.md) as the execution record, rather than rewriting unverified gates as successes.
