# A working day with Work Copilot

Use the dashboard for orientation and review. Keep your notes in your selected vault and your writing/coding in the tools you already use. Work Copilot keeps the shared record; it does not replace those tools or send on your behalf.

New installation? Start with [Getting started](GETTING_STARTED.md). Use fictional data until the [local readiness checks](LOCAL_READINESS.md) pass for your setup.

## Set up a project

Open **Projects → Create project**. For example:

- Stable ID: `atlas`
- Name: `Atlas launch`
- Aliases: `Atlas pilot, AT launch`
- Room IDs: the real stable identifiers your approved source adapter supplies, not guessed display names

A project may have several rooms. A shared delivery room may belong to Atlas and Beacon. Do not create duplicate projects just because commercial, product, and engineering discussions happen separately: assign **workstreams** when capturing or mapping their evidence.

Use **Edit project** on its project page to revise the name, aliases, and room bindings; the stable ID remains fixed. Exact configured room IDs route incoming sources only when explicit source project mappings are absent. A shared binding can produce multiple project links; explicit mappings take precedence. This is local routing, not room discovery: it does not create or join rooms, grant access, or start a vendor integration. Aliases still provide suggestions for review, not confirmed assignment.

## Morning: decide what deserves attention

1. Open **Today**. **Needs you** lists owned open work; **Waiting** shows dependencies. Candidates with uncertain ownership belong in **Review queue**.
2. Read **Today, in focus** as a suggested plan, not a promise. Use **Choose today’s focus** to pin up to three owned open actions in your chosen order. The plan stays anchored rather than silently reshuffling; completed/dismissed/waiting items leave focus without auto-filling the gap. Look separately at **New urgency**.
3. Check **Sources & coverage**. A quiet queue only describes captured sources, not every inbox or room. Errors, sleep, and unavailable transcripts create gaps.
4. Use **This week** for dated commitments versus suggested work. An undated item is not automatically this week's commitment. The **Overdue, including earlier weeks** lane keeps missed dates visible. Expand undated/other dates for the broader queue.

The tracking form lets you record a waiting-on person/dependency, optional promised due date, and explicit commitment flag. Leave these empty/unchecked when unsupported. They are separate from a follow-up reminder; not every tracked action is a promise.

A follow-up reminder means “check this again.” A deadline means a source or an explicit record actually supplied a due date. Do not turn one into the other. Follow-ups are visible in the app; this prototype does not deliver OS or email reminder notifications.

## After a meeting: keep the original, then reconcile

Click **Capture a note**, paste a note or transcript, and optionally select its project and workstream. Alternatively, save a `.md` file anywhere in the selected work folder. The service captures selected Markdown while running; **Refresh** requests another capture.

You do not have to tag every original perfectly:

- Plain text is retained as source context with line references.
- Exact project names, IDs, and configured aliases can propose mappings. Shared aliases remain ambiguous.
- Review the suggestion; it is not an automatic project assignment or proof that you own an action.
- In **Sources & coverage** or source evidence, use **Map to projects** to confirm all relevant projects/workstreams. Preserve shared links rather than choosing only one.

Decisions, risks, and background awareness can remain useful source text without becoming tasks. This prototype does not semantically extract a complete task list from arbitrary prose. Explicitly **Track waiting / action**, or ask your approved agent to read the evidence and propose structured records.

If the transcript has not arrived, track “Waiting for the Atlas transcript.” Do not create a recap from an absent source.

## When someone needs a reply

1. Open the relevant attention card and expand **Evidence**. Check the source revision, observed date, coverage, and missing information.
2. Choose the optional **Prepare reply** action when a response is needed. **Review / update** is the general action for development, ticket work, and other obligations. The built-in result is an **offline template**, not model-generated reasoning. It exposes evidence and gaps for review.
3. Edit locally, or ask your approved agent to draft from the same project/request context. A useful request is: “Use `get_reply_context` for this obligation, then `get_source` for missing passages. Help answer using current evidence; distinguish confirmed facts, unknowns, and questions already asked.”
4. If new evidence makes the draft stale, prepare it again. Do not reuse an old answer because the prose still sounds plausible.
5. Copy the reviewed reply and send it in the original tool, after checking the destination and source-sharing permissions.
6. Only then, if useful, record **Sent elsewhere** in the question state. Copying, saving, or reviewing never sends anything. Acknowledging a question does not resolve the underlying work.

For engineering replies, keep claims separate: **planned → implemented → merged → tested → deployed** are not interchangeable. A Jira issue can supply high-level status or ownership; a GitHub PR/check supplies specific code/test evidence. Neither implies a production deployment unless an appropriate receipt supports it. These are evidence roles, not claims that live Jira/GitHub adapters are included.

## Track a dependency without creating duplicate asks

Choose **Track waiting / action**:

- Describe the distinct request.
- Use a stable intent key, such as `atlas-workshop-transcript`.
- Choose project, owner, and state; leave uncertain ownership unresolved.
- Set an optional follow-up time. Record a promised due date and mark an explicit commitment only when warranted. Name the waiting-on dependency if known. Add supporting context or source references where available.

Reuse the same project/intent for the same request. Different questions in one room need distinct intents. For example, “build the skill,” “update the Jira ticket,” and “send the team a status answer” are separate obligations: completing code does not complete the ticket update or inform the team. The app deduplicates explicit keys, not arbitrary similar prose. Review pending/sent/answered state before asking again.

### Example: three projects, nine distinct obligations

| Project | Development intent | Ticket intent | Stakeholder intent |
| --- | --- | --- | --- |
| Atlas | `atlas-build-skill`: implement the skill | `atlas-ticket-update`: update its Jira record | `atlas-team-answer`: answer the delivery room |
| Beacon | `beacon-build-check`: implement the check | `beacon-ticket-update`: update the tracked issue | `beacon-product-answer`: answer the product question |
| Cedar | `cedar-build-report`: implement the report | `cedar-ticket-update`: update the status record | `cedar-commercial-answer`: share the agreed scope |

These are fictional examples, not preconfigured integrations or a new task-type engine. Track only the obligations actually present. A merged PR may support the development record; it does not automatically update Jira, answer a team, prove deployment, or resolve the other intents. Review each obligation against its own evidence.

An agent can also use the shared local tools. Ask it to return the record/tool receipt, not merely say it remembered. A natural-language “tomorrow” must become an explicit timezone-aware timestamp in the structured call.

## End of day: preserve context for the next session

Capture a short handoff: what changed, which sources support it, what remains unknown, and who you are waiting on. Include actual commit/test/issue references when relevant. A session report is a claim to verify; it does not prove deployment or notification.

Use the **same data directory and timezone** for CLI, dashboard, and MCP. MCP note capture additionally requires `WORK_COPILOT_VAULT` set to the selected folder; merely opening a vault in the browser does not configure another process. See [client setup](CLIENT_SETUP.md).

Tomorrow's agent session can retrieve the saved project and existing obligations instead of rebuilding everything from chat. Use [the two-session smoke test](VALIDATION.md#your-two-session-local-agent-smoke-test) to verify this with your actual client.

## Short-term attention versus durable project knowledge

| Layer | What belongs there | How to correct it |
| --- | --- | --- |
| **Today / waiting / drafts** | Current attention, follow-ups, question state, local draft revisions | Revision-checked review or explicit structured tools |
| **Original vault notes and upstream sources** | Human-authored notes, transcripts, decisions and authoritative external records | Edit the original in its owning tool; selected vault edits are recaptured |
| **SQLite source/operational store** | Stable identities, captured revisions, mappings, obligations and checkpoints | Service/API/MCP operations, not direct database editing |
| **`Work Copilot Generated/`** | Readable project/source/review projections for Obsidian | Do not maintain a competing editable wiki here; correct the original or mapping, then regenerate |

Durable context is not hidden model memory, and a captured correction is not automatically an accepted learning or updated skill.

Open the generated Markdown in Obsidian to inspect project context and source passages. Generated context is a view of observed evidence, not independently verified truth. Keep handwritten notes outside the generated folder. In-place note edits retain identity; moving/renaming currently behaves as deletion plus a new source.

## When something looks wrong

- **Wrong project/owner:** review the mapping or obligation; do not rewrite history by editing generated output.
- **Source changed after a draft:** refresh and regenerate before copying.
- **No expected results:** loosen lexical search filters, then inspect coverage and the selected vault. An absent result is not proof the event never happened.
- **Auth error:** follow [OAuth MCP setup](SYMPHONY_MCP.md); a successful login alone does not prove catch-up completeness or ACL coverage.
- **Local setup unclear:** run `./run.sh --data-dir /absolute/private/copilot --timezone America/New_York doctor --vault /absolute/path/to/work-vault`, replacing the example paths/timezone with those used by your running service. Diagnostics inspect local setup, not company authorization or end-to-end agent behavior.
