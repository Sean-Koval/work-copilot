# Design and workflow review

Reviewed October 26, 2026 against the local MVP and its install-readiness continuation. This is a code, test, and synthetic-workflow review, not a workplace trial or visual usability study.

## Bottom line

Keep the small SQLite ledger, Markdown knowledge views, and shared MCP interface. A graph database, separate memory service, or multi-agent runtime is not needed to make the next version useful.

The product must distinguish **doing development**, **maintaining its ticket**, and **answering a stakeholder**. These are separate obligations, even when they relate to the same project. A merged pull request cannot silently complete all three.

The current tool is a local capture, retrieval, and follow-through assistant. It does not automatically understand every message or run an always-on language model. Raw source collection, exact alias suggestions, structured obligations, and agent-assisted interpretation are different capabilities.

## The scenario that matters

Consider three fictional team engagements. Two are behind on both development and communication. The user should be able to:

1. See overdue work and who is waiting without opening every source.
2. Pick up to three realistic priorities, rather than have the plan constantly reshuffle.
3. Read current project evidence and prepare an honest update without inventing an ETA.
4. Track a development action, ticket-update action, and stakeholder response separately.
5. Capture a meeting note once, associate it with the appropriate projects, and retain its original text.
6. Return in a fresh agent session and retrieve those same records.

This scenario is the acceptance target, not the number of dashboard panels or passing unit tests.

## Findings and the readiness response

| Initial finding | Why it mattered | Response in the continuation |
| --- | --- | --- |
| All owned actions were labeled as responses | Development work looked like another message to answer | Neutral attention wording and separate-intent guidance |
| Previous-week overdue commitments were buried in backlog | A weekly view could conceal the most urgent unfinished work | Explicit overdue presentation |
| Completed and waiting items could linger in focus | The daily plan stopped reflecting actionable work | Active focus filtering and revision-checked manual priority selection |
| The form hid deadline, commitment, and waiting fields | Routine capture required knowing MCP payloads | Expose supported fields in the local workflow |
| Room configuration was display-only | Adding room IDs did not route incoming evidence | Deterministic configured-room routing, explicit mappings taking precedence, and editable project configuration |
| A large urgency list defeated packet pagination | With 900 overdue items, even a page size of one exceeded the packet budget | Independently bounded urgency lists, counts, and continuation |
| Project continuation required global paging | Finding the rest of one project's obligations was unnecessarily expensive | Project-filtered attention retrieval |
| Direct evidence was repeated in reply context | Repetition consumed context without adding information | Direct-first, deduplicated evidence selection |
| Diagnostics could initialize or modify state | A health check was not actually read-only | Path-based diagnostics with isolated SQLite inspection |

These changes need to be read alongside the executed results and remaining gaps in [Validation](VALIDATION.md). A documented response is not evidence of a successful company integration.

## Token and computation costs

The initial review measured a fictional three-project, nine-obligation fixture containing short English identifiers and approximately 6.3 KB source bodies:

| Payload | UTF-8 bytes | Rough characters divided by four |
| --- | ---: | ---: |
| Default attention | 4,455 | approximately 1,114 |
| Project context | 2,813 | approximately 703 |
| Reply context | 2,927 | approximately 732 |
| Eight MCP tool definitions | 6,347 | approximately 1,587 |

These are historical synthetic payload measurements from the review, **not tokenizer counts, billing estimates, or a benchmark of the revised release**. Agent instructions, chat history, tool schemas, repeated calls, and model output add their own cost. The app itself does not call a paid model by default.

The important design rule is to query exact open obligations first, then retrieve bounded relevant evidence. Search results are not the task ledger: a search miss must never delete or hide a commitment. Pagination must retain counts and a usable continuation path.

Some internal operations still materialize broad snapshots, rebuild full-text state at startup, or rewrite generated Markdown views. This is acceptable only at small-pilot scale until measured otherwise. Prefer narrow SQL reads and incremental work before introducing another database or service.

## What is deliberately not solved

- **Semantic capture:** arbitrary prose does not reliably become an obligation without structured input or the user's agent interpreting it. Automatic incoming-message triage needs a separately configured, evaluated extraction path.
- **Turnkey workplace connections:** OAuth protocol support does not supply a company's endpoint, scopes, client registration, read-tool schema, cursor semantics, or authorization policy.
- **Capacity-aware scheduling:** manual priorities and weekly grouping are not estimates of available hours or a scheduling optimizer.
- **Team learning governance:** proposed correction context is not an accepted global rule or an automatically rewritten skill.
- **Complete source truth:** current source access, deletion coverage, retention and enterprise ACL behavior require deployment-specific validation.
- **Proactive delivery:** follow-ups appear in the app; they are not push, email, or chat reminders unless a separate delivery feature is configured and authorized.

## Keep versus defer

**Keep:** one canonical local store; explicit project identities; many-to-many source relationships; original notes and evidence links; safe restart and deduplication; visible coverage gaps; user-reviewed responses.

**Improve next using observed failures:** first-run clarity, one verified source profile, semantic-action recall on a manually labeled sample, and the time required to produce an accurate stakeholder update.

**Defer until evidence warrants it:** graph databases, embedding infrastructure, autonomous background agents, a workflow engine, and tight dependencies on other agent platforms. Reuse a tool only when it removes more ongoing work than it introduces.

## A practical user trial

Start with fictional data and the [getting-started flow](GETTING_STARTED.md). Then, after approving work-data access, choose one real engagement and a bounded set of rooms. Compare the tool's queue with a manual check. Record missed actions, false assignments, duplicate questions, stale status claims, and time to a usable reply. Include an overdue item, a changed deadline, a missing transcript, a shared room, and a merged-but-not-deployed change.

Passing that trial would be stronger evidence of value than simply adding more integrations.
