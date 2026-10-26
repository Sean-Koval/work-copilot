# Work Copilot

**Remember the context. Know who needs a reply. Keep your own workspace.**

Work Copilot is a local-first companion for people juggling projects across rooms, notes, meetings, and engineering tools. It gathers selected evidence, keeps a durable attention queue, and helps you prepare a grounded response without rereading every conversation.

Use the localhost dashboard to see what needs you; keep Claude or Antigravity for thinking, writing, and coding. Both can use the same local context through MCP. There is no required cloud account or paid model, and **the app never sends messages or changes external work systems**.

**Status:** working proof of concept, not a certified workplace deployment. Linux/Python 3.12 has recorded local validation. Your laptop, chosen agent, and company connectors still need their own checks. See [local readiness](docs/LOCAL_READINESS.md).

## Try it with fictional work

Requires Python 3.11+ and internet access for the first dependency installation.

```sh
git clone https://github.com/Sean-Koval/work-copilot.git
cd work-copilot
./run.sh smoke   # isolated fictional-data check
./run.sh         # open the persistent demo
```

Open **http://127.0.0.1:8765**. Stop with Ctrl+C. Restarting keeps the demo state. No login service is installed.

Windows PowerShell: `./run.ps1`. This launcher is provided, but Windows and macOS runtime checks remain unverified. [Getting started](docs/GETTING_STARTED.md) covers installation, private storage, timezone, diagnostics, and agent setup.

## What it helps with

| When work feels like… | Use Work Copilot to… |
| --- | --- |
| “I owe people replies, but need the context first.” | Inspect **Needs you**, open the source evidence, and prepare/edit/copy a draft. Templates run offline; ask your approved agent for richer writing. |
| “One project is spread across five rooms.” | Link multiple rooms to one project, including commercial, product, and engineering workstreams. A shared room can also support several projects. |
| “The useful detail is buried in messy notes.” | Capture Markdown notes/transcripts, search their original text, and review narrow project-alias suggestions instead of silently guessing ownership. |
| “I’m waiting on something and will forget.” | Track a canonical request and a follow-up reminder separately from a promised deadline. |
| “A new chat/session loses everything.” | Keep operational state in SQLite and inspect project/source projections in your selected Obsidian-compatible Markdown vault. |

**Collection is not automatic understanding.** Raw messages and notes remain source context. Obligations come from explicit intake, structured source fields, or your chosen agent proposing tool calls. No always-on LLM infers every task, decision, or reply owed.

## Start your real workspace

Choose an approved, dedicated **work-only Markdown folder**, not your whole personal vault:

```sh
./run.sh --timezone America/New_York serve --vault /absolute/path/to/work-vault
```

1. **Create project**: give it a stable ID, readable name, aliases, and room IDs.
2. **Capture a note** or drop a Markdown transcript into the selected folder. Choose a project/workstream or leave the mapping for review.
3. **Review queue**: confirm ambiguous projects and owners. A mention does not assign work to you.
4. **Track waiting / action**: record the distinct request and optional follow-up time.
5. **Prepare reply**: inspect freshness and evidence, then edit/copy. Send only in your original tool.

For a practical morning-to-evening routine, read [Daily workflow](docs/DAILY_WORKFLOW.md).

Demo storage and real storage are separate by default: `~/.local/share/work-copilot-demo` and `~/.local/share/work-copilot` (under `$XDG_DATA_HOME` when set). Use `--data-dir` **before** the command to select another private location. Never put real data in a public checkout or sync a live SQLite database.

## Your notes remain yours

Handwritten originals are preserved. The selected folder is scanned recursively for Markdown; editing in place preserves source identity, while moving/renaming currently produces a deletion plus a new source.

`Work Copilot Generated/` contains readable project views, source passages/citations, and review context. It is a rebuildable projection, **not a second editable task list or authoritative set of facts**. Correct original notes or use revision-checked local reviews. Source links and revisions let you inspect what a statement is based on.

Today’s priorities begin as suggestions; pin up to three owned open actions in your chosen order. The daily plan stays anchored and new urgency is surfaced separately. This Week distinguishes dated commitments from suggested work. A merged PR is not deployment evidence. A missing transcript remains missing, not a fabricated meeting summary.

## Connect only what is approved

- **Local MCP:** expose shared context to your supported Claude/Antigravity client. [Client setup](docs/CLIENT_SETUP.md) has explicit local MCP configuration; [validation](docs/VALIDATION.md) includes a two-session recall test. Client-specific configuration and model-agent operation have not been verified here.
- **Company OAuth MCP:** explicitly configure an approved HTTP endpoint and allowed read tools. [Symphony MCP setup](docs/SYMPHONY_MCP.md) explains OAuth, credential storage, discovery, and normalization. The protocol was tested with a local fake server, **not your company server**.
- **Jira, GitHub, Confluence, meetings:** vendor-specific live adapters are not implemented. Approved normalized JSON exports/adapters can supply their evidence using the [connector contract](docs/CONNECTORS.md). Fictional demo provider labels do not mean connected accounts.
- **Session handoffs:** [local hooks/intake](docs/HOOKS.md) accepts explicit activity receipts. No private agent traces are scraped or client hooks auto-installed. A receipt is a claim to verify, not proof that work shipped or a stakeholder was informed.

The configured collector runs while the service is running. Closing the browser does not stop it; closing the service or sleeping your laptop does. Inspect **Sources & coverage** for gaps and errors rather than assuming all work is observed.

## Important boundaries

- Loopback-only, single-user app; local browser write protections are not remote authentication.
- No external send operation, automatic OS permissions, cloud deployment, or paid model call.
- Work content is stored locally without application-level encryption. Use approved storage and OS protection. Enterprise retention, backup, and live upstream ACL enforcement are not implemented.
- Search is ranked lexical retrieval, not semantic search. Alias matches are suggestions, not semantic entity resolution. Contradictory prose is not automatically reconciled into proven facts.
- Before using work content with an agent, confirm your organization permits the client, model, and data handling.
- No license has been selected. Public source availability alone does not grant an open-source license.

## Development and evidence

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[test]'
.venv/bin/python -m pytest -q
```

Dependencies are pinned; `constraints.txt` records the tested set. Launchers install initially or after a dependency change, rather than running pip on every unchanged launch.

The current local-readiness validation has **140 passing tests** (the original MVP record had 79), actual local SDK client/server tests, fake-server OAuth tests, and fresh-install/HTTP restart checks. It does **not** include successful browser visual QA, a real model-agent session, or your company server. [Validation details](docs/VALIDATION.md) separate executed checks from blocked/unverified checks. [Local readiness](docs/LOCAL_READINESS.md) defines the remaining acceptance gates without claiming a “1.0” release.

The [architecture proposal](docs/ARCHITECTURE.md) describes longer-term possibilities; hosted deployment and external actions there are not current features.

### Further reading

- [Design and product review](docs/DESIGN_REVIEW.md): fit, retrieval costs, resolved defects and deferred work.
- [Open-source research](docs/OSS_RESEARCH.md): comparison and primary references, not extra runtime dependencies.
