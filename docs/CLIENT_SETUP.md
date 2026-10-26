# Use your existing coding/writing agent

The dashboard and your agent read **one private Work Copilot database**. Connecting a client does not connect Symphony, send a message, grant a work account, or start a model inside Work Copilot. Your chosen agent may have its own subscription/data policy.

First pass [Getting started](GETTING_STARTED.md) and `./run.sh smoke`. Then choose **one** client below. These are instructions for you to run on your laptop; no client configuration has been installed by this project.

## Keep these three values identical

| Setting | Example placeholder | Where it must agree |
|---|---|---|
| Private state directory | `/absolute/private/copilot` | Dashboard `--data-dir` and MCP `--data-dir` |
| Timezone | `America/New_York` | Dashboard and MCP `--timezone` |
| Approved work-only Markdown folder | `/absolute/work-vault` | Dashboard `--vault`; MCP environment `WORK_COPILOT_VAULT` |

Replace placeholders with your own absolute paths. The Python executable is the one installed by this checkout: `/absolute/work-copilot/.venv/bin/python` on Linux/macOS or `.venv\Scripts\python.exe` on Windows. Windows execution remains unverified here. Do not put secrets or real work paths in a public repository.

## Codex CLI

Codex supports a local stdio command and explicit environment entries through `mcp add`. The syntax below was checked against the installed CLI help (0.163.0) and [official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Running `add` changes your client configuration; inspect it first.

```sh
codex mcp add work-copilot \
  --env WORK_COPILOT_VAULT=/absolute/work-vault \
  -- /absolute/work-copilot/.venv/bin/python -m work_copilot \
  --data-dir /absolute/private/copilot --timezone America/New_York mcp
codex mcp get work-copilot
```

Use shell quotes around paths containing spaces. Removing only this client entry later uses `codex mcp remove work-copilot`; it does not delete Work Copilot data. If you prefer configuration files, the equivalent entry is:

```toml
[mcp_servers.work-copilot]
command = "/absolute/work-copilot/.venv/bin/python"
args = ["-m", "work_copilot", "--data-dir", "/absolute/private/copilot", "--timezone", "America/New_York", "mcp"]

[mcp_servers.work-copilot.env]
WORK_COPILOT_VAULT = "/absolute/work-vault"
```

A saved entry is not a successful model-agent test. The previous cloud-runtime attempt failed before model/MCP startup due to the execution environment; no bypass/retry is built into this app. Verify on your own approved runtime with the two-session exercise below.

## Claude Code

Claude Code supports stdio servers, a private local scope tied to the current project, and server inspection. This follows the [official Claude Code MCP reference](https://code.claude.com/docs/en/mcp); it is **not** a claim that a real Claude session was tested here.

From the project directory where you want the connection available:

```sh
claude mcp add --transport stdio --scope local \
  --env WORK_COPILOT_VAULT=/absolute/work-vault work-copilot \
  -- /absolute/work-copilot/.venv/bin/python -m work_copilot \
  --data-dir /absolute/private/copilot --timezone America/New_York mcp
claude mcp get work-copilot
```

In Claude Code, inspect `/mcp` and any requested permissions. A local scope is intentionally not a team-wide project configuration. Review each local-write tool action; no tool in this app can send externally. Do not substitute a web URL for the stdio command.

## Antigravity or another MCP client

Use its verified **local stdio MCP** setup interface with the same executable, argument array, and environment shown above. This repository has not verified Antigravity's native configuration file or hook lifecycle schema, so it does not invent one. If your client cannot launch this command, run the dashboard and use explicit note/activity intake instead while checking that client's documentation.

## What to ask your agent

- “Show my open replies owed and waiting items. Cite the source IDs; do not create new commitments.”
- “Capture this transcript for project `atlas`, workstream `engineering`; retain decisions and risks. Propose actions separately for review.”
- “Track that I am waiting on Alex for the transcript. Use canonical intent `atlas-transcript`, follow up on **an explicit date/time with timezone**, and do not send a request.”
- “Draft a reply using the existing intent and recent evidence. Do not infer deployment from a merged PR.”

The agent interprets your request, then calls structured local tools. Raw collected text does not automatically become an accurately extracted to-do list. Verify tool receipts and IDs, not just a conversational claim that a task was saved.

## Two sessions, one persistent memory

Follow the [two-session fictional exercise](VALIDATION.md#your-two-session-local-agent-smoke-test): create/capture/track in Session 1, close the client, then recall/search the same IDs in Session 2 without pasting earlier context. Keep the same data directory. The automated `smoke` checks SDK persistence, **not** a model's reasoning or the client's configuration.

## Optional explicit handoff — no native hook required

Ask your agent to produce a small structured activity JSON using [HOOKS.md](HOOKS.md), review it, then run:

```sh
./run.sh --data-dir /absolute/private/copilot activity --file /private/receipt.json --queue-only
./run.sh --data-dir /absolute/private/copilot drain-activity
```

Only the supplied receipt is ingested. No private session/trace directories are read. Outcome claims, procedure references and correction learnings do not verify a deployment, inform a stakeholder, execute a procedure, or become accepted global rules.

Claude's official [hooks reference](https://code.claude.com/docs/en/hooks) defines lifecycle payloads, including a Stop re-entry flag. Those payloads are **not** Work Copilot's activity schema. No native Stop hook adapter is provided or installed: do not pipe arbitrary native payloads or transcript paths into this intake. A future adapter must deliberately map permitted fields and guard recursion; the explicit reviewed handoff above works without that dependency.
