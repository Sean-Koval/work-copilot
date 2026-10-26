# Install and try Work Copilot locally

This is an early laptop prototype. **Python 3.11+ is required. Linux/Python 3.12 has been exercised; macOS, Windows, and your particular Claude/Antigravity client still need verification.** The first install downloads ordinary Python dependencies. Normal unchanged launches and the synthetic smoke test need no cloud account, paid model, or work credentials.

## 1. Install, then run the isolated check

From a terminal with Git and Python installed:

```sh
git clone https://github.com/Sean-Koval/work-copilot.git
cd work-copilot
python3 --version
./run.sh smoke
```

The launcher creates a project-local `.venv` and installs the pinned dependency set on first use. It does not install a login service or request OS permissions. Read `bootstrap.py` and `constraints.txt` if you want to inspect installation first.

`smoke` creates only fictional projects, notes and a database in a temporary directory, checks capture/search/follow-ups/drafts/wiki/restart, and initializes the official MCP SDK over **local stdio**. The directory is cleaned up afterward. It does not read your vault, connect to a work system, or call a model. Expect `"success": true`; failures identify a phase and the command exits nonzero. `--data-dir`, if supplied, is ignored for this isolated check. This is not a test of your actual agent client or company OAuth server.

**Windows PowerShell (provided, not runtime-verified):** use `./run.ps1 smoke`. Alternatively avoid a shell launcher:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe bootstrap.py
.\.venv\Scripts\python.exe -m work_copilot smoke
```

Check `py -3 --version` is 3.11 or newer first. macOS uses the shell commands above with an installed Python 3.11+, but is also unverified. No instructions here require changing execution policy or global Python packages.

## 2. Explore fictional work

```sh
./run.sh demo
```

Open **http://127.0.0.1:8765**. Review Today, This Week, project rooms/workstreams, waiting items, source passages and an offline reply draft. Copying or marking a local question sent does not send a message. Stop the service with Ctrl+C; restarting retains demo state.

Demo state defaults to `~/.local/share/work-copilot-demo`; real state uses the separate `~/.local/share/work-copilot` (or the equivalent directories under `$XDG_DATA_HOME`). Neither belongs in the public checkout. To choose another private location, put `--data-dir` **before** the command:

```sh
./run.sh --data-dir /absolute/private/copilot-demo demo
```

## 3. Start an empty real workspace

Choose an existing, approved **work-only Markdown folder**. Capture is recursive; do not select your whole personal vault. Choose your own IANA timezone; UTC is the default.

```sh
./run.sh --timezone America/New_York --data-dir /absolute/private/copilot serve --vault /absolute/work-vault
```

1. Create a project in the dashboard with a stable ID, display name, known aliases and room IDs. A project can have several rooms; a room can belong to several projects. This creates local metadata, not remote rooms.
2. Paste a short permitted note/transcript with **Capture note**, selecting its project/workstream, or drop a UTF-8 `.md` file into the selected folder.
3. Review unassigned mapping/ownership candidates. Inspect the original source and the separate `Work Copilot Generated/` project wiki. Handwritten originals are preserved; generated projections are not a second editable task list.
4. Track one waiting item and its follow-up date. Follow-up reminders are distinct from promised deadlines. Use a draft's evidence packet before copying a reply.

Raw notes and remote messages become **context**, not an automatically extracted, complete task list. Obligations require explicit structured intake, manual review, or your approved agent reading evidence and calling the local tools. No background LLM extracts every request or sends replies.

See [Daily workflow](DAILY_WORKFLOW.md) for the regular loop and [connector/metadata contract](CONNECTORS.md) for optional structured intake.

## 4. Use the same state from an approved agent

After installation, add a stdio MCP server using your client's supported configuration interface. This is a conventional configuration example, **not a verified installation recipe for a specific client/version**. Use absolute paths, the same real data directory as the dashboard, and your actual timezone:

```json
{
  "mcpServers": {
    "work-copilot": {
      "command": "/absolute/work-copilot/.venv/bin/python",
      "args": ["-m", "work_copilot", "--timezone", "America/New_York", "--data-dir", "/absolute/private/copilot", "mcp"],
      "env": {
        "WORK_COPILOT_VAULT": "/absolute/work-vault",
        "WORK_COPILOT_TIMEZONE": "America/New_York"
      }
    }
  }
}
```

On Windows the Python executable is `.venv/Scripts/python.exe` (escape backslashes if using them in JSON). Omit `WORK_COPILOT_VAULT` if you do not want the agent to have the local `capture_note` tool. Explicit local tools can create projects, capture selected notes, track obligations and review local records. They cannot authorize or send external messages.

Ask the client to list projects, retrieve one source passage, then track a fictional waiting item before using sensitive work context. Inspect the receipt and dashboard. The client—not this service—interprets natural language and chooses its model. Running this store locally does **not** guarantee your client's model processes data locally; obtain the required company approval first.

## 5. Diagnose before connecting work systems

```sh
./run.sh --data-dir /absolute/private/copilot doctor --vault /absolute/work-vault
```

Doctor inspects local readiness; it does not authorize or contact a server. For an approved Symphony OAuth MCP endpoint, follow [Symphony MCP setup](SYMPHONY_MCP.md). Endpoint tools, scopes and response schema must be verified against your actual server. No vendor-specific Jira/GitHub/Confluence integration or live company-server test is implied by the fictional demo or smoke result.

### Common first-run issues

- **Python/venv missing:** install Python 3.11+ through your normal approved process. Some Linux distributions package venv separately. No global installation is performed by Work Copilot.
- **Dependency download fails:** first installation needs access to your approved Python package index. Fix that access rather than disabling TLS or copying credentials into the repository.
- **Port already used:** choose `./run.sh demo --port 8766`, then open `http://127.0.0.1:8766`.
- **Wrong/empty state in the agent:** compare absolute `--data-dir` values; demo and real state are intentionally separate.
- **No capture tool in the agent:** set `WORK_COPILOT_VAULT` explicitly to an existing permitted folder, then restart that MCP process using the client's controls.
- **Vault rejected:** symlinked roots/paths are not supported. Select an actual work-only folder; do not broaden filesystem permissions blindly.
- **Smoke fails:** keep its failing phase and error when reporting the problem. The synthetic check contains no work payload; review any additional logs before sharing. A successful smoke is not proof of live connector or actual-client compatibility.

The app does not encrypt your SQLite/Markdown files, supply backups, enforce live enterprise ACLs, or stay awake when the laptop sleeps. Use approved local storage and keep private state out of Git. Nothing in these steps deploys a cloud service or installs a startup agent.
