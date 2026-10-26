# Agent memory that reduces work, rather than creating another inbox

**Research checked 26 October 2026. Recommendation: retain the local work ledger; reuse retrieval and knowledge tooling selectively.**

The highest-value distinction is between **remembering information** and **keeping a promise**. A searchable memory can explain a decision, but cannot establish that a ticket was updated, a reply was sent, or a retry is safe. For a multi-project work copilot, prioritize reliable capture, explicit project state, and a reviewable communication queue before autonomous memory consolidation.

## Decision in brief

- **Adopt now as design patterns:** source-linked Markdown knowledge, bounded context packets, explicit work-state transitions, and reviewed procedural notes. Avoid requiring daily manual note-taking.
- **First isolated evaluation:** QMD as a replaceable, read-only retrieval sidecar over exported Markdown. Keep SQLite/FTS as the baseline and fallback.
- **Evaluate later:** Basic Memory if replacing—not duplicating—the note/MCP subsystem saves maintenance; Mem0 for extracted personal facts; Graphiti only for demonstrated temporal/multi-hop failures; TeamAI for genuinely shared coding procedures.
- **Skip as core architecture now:** PageIndex for short work events, Letta as a second agent runtime, and DBOS until durable multi-step execution justifies PostgreSQL.

These are integration judgments, not benchmark rankings. No candidate was installed or tested here; no paid calls or private-data ingestion occurred. The fit assessment uses the supplied work-copilot architecture brief, not a fresh audit of its public repository.

## Separate five kinds of state

| Layer | Store and retrieve | Do not confuse it with |
|---|---|---|
| Working context | Current objective, selected evidence, pending tool results; bounded per session | Permanent truth |
| Episodic evidence | Timestamped source events, IDs, revisions, source links, access scope | Model-generated summaries |
| Semantic knowledge | Claims, decisions, entities, uncertainty, supersession | Current task status |
| Procedural knowledge | Reviewed runbooks, triggers, exceptions, successful examples | Automatically trustworthy instructions |
| Operational state | Owners, dependencies, due dates, approvals, execution receipts | Similarity-search results |

For the existing SQLite/FTS + Markdown/Obsidian + shared MCP design, keep authoritative work state structured. Make knowledge pages reproducible projections or explicitly human-owned documents; do not let two tools silently become concurrent owners of the same fields. Jira can remain high-level planning and GitHub implementation evidence. A shared MCP interface makes context accessible across clients, but does not itself synchronize their chat histories or prove a source adapter works against the real server.

## Focused candidates

### Basic Memory: reusable note infrastructure, not an autonomous project manager

**Evaluate later / adopt the file-first pattern now.** Basic Memory provides human/agent-editable Markdown, wikilink relationships, search and MCP. Local Python tooling and SQLite avoid a mandatory database service; semantic search/reranking adds model dependencies. A client LLM still spends tokens reading and writing notes, and local storage does not prevent that client sending retrieved content to its provider. Cloud synchronization is a separate hosted choice. [Tagged README](https://raw.githubusercontent.com/basicmachines-co/basic-memory/v0.24.1/README.md)

Its useful replacement target is the custom note CRUD/index/MCP layer—not ticket reconciliation, reply ownership or transactional execution. Adding it beside an equivalent subsystem would create duplicate indexes and conflicting write paths. Current released **v0.24.1, October 23**, has **AGPL-3.0**, not an assumed permissive license; review the actual embedding/distribution/service arrangement before adopting code. This is a licensing review flag, not a blanket incompatibility claim. Active releases demonstrate maintenance, not operational reliability in this app. [Release](https://github.com/basicmachines-co/basic-memory/releases/tag/v0.24.1), [tagged license](https://raw.githubusercontent.com/basicmachines-co/basic-memory/v0.24.1/LICENSE)

### Karpathy's LLM Wiki: a pattern, not a package

**Adopt selectively now.** The actual source is an April 4 **idea-file gist**, not an official `karpathy/llm-wiki` product repository. It separates immutable sources, generated interconnected Markdown, and an agent-maintained schema; ingest, query and lint are distinct operations. It can run with an existing coding client and files, without another server. [Original gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

Precomputing synthesis may reduce repeated reading, but ingestion, cross-page updates and linting consume tokens and can propagate mistakes. Keep claim-level citations, source revisions and reviewable diffs. Do not copy the rhetoric that maintenance costs are near zero. It supplies neither connectors nor permissions nor an execution ledger. No explicit reusable software license was established for the gist; use the architectural idea rather than assume a licensed implementation. Retain ordinary search: synthesis and retrieval complement each other.

### Mem0: fact extraction and recall, with an important OSS/Platform split

**Evaluate later**, only if repeated fact capture is a measured pain. Inspected **Python v2.2.1** implements the newer **V3 algorithm**—package version and algorithm version are different. The migration specifies single-pass **ADD-only automatic extraction** and hybrid retrieval; external graph-store integration moved out of OSS to Platform. Explicit update/delete APIs still exist. Do not use older automatic ADD/UPDATE/DELETE or OSS graph tutorials as the implementation contract. [Tagged migration](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/docs/migration/oss-v2-to-v3.mdx), [implementation](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/mem0/memory/main.py)

Minimum components: SDK, history storage, configured vector store, extraction model and embeddings. Defaults select OpenAI for both model paths and Qdrant for vectors; self-hosting memory is not automatically local inference. Cost comes from extraction input/output, embeddings, retrieval/reranking and storage. [LLM config](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/mem0/llms/configs.py), [embedding config](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/mem0/embeddings/configs.py), [store config](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/mem0/vector_stores/configs.py)

It does not replace canonical project state or guarantee contradiction resolution. Keep extracted claims tentative until provenance and supersession tests pass. **Apache-2.0**; September 25 release fixes rejected vector writes incorrectly reported successful—an instructive integration risk. [License](https://raw.githubusercontent.com/mem0ai/mem0/v2.2.1/LICENSE), [release](https://github.com/mem0ai/mem0/releases/tag/v2.2.1)

### Graphiti: relationships that change over time

**Evaluate later** for questions such as “who owned this dependency when that decision was made?” It combines episodes, entities, relationships, temporal validity and provenance. Python plus a supported graph backend and LLM/embedding providers are required; Neo4j or FalkorDB are current options. Kuzu is deprecated. Default inference is hosted; local compatible endpoints require testing structured-output quality. Ingestion/extraction and embeddings cost compute/tokens, even when retrieval avoids generative summarization. [Tagged README](https://raw.githubusercontent.com/getzep/graphiti/v0.31.2/README.md)

This adds another database, extraction errors and ontology maintenance; it does not prove a task is complete or resolve conflicting authorities by itself. Start with explicit SQL event history before paying this complexity tax. **v0.31.2, October 20; Apache-2.0**. Release notes explicitly describe MCP/REST servers as reference-only and tighten localhost binding: do not treat the example server as production authorization infrastructure. [Release](https://github.com/getzep/graphiti/releases/tag/v0.31.2), [license](https://raw.githubusercontent.com/getzep/graphiti/v0.31.2/LICENSE)

### Letta: persistent agent identity and memory, but a runtime choice

**Skip replacing the current clients now; evaluate separately if an always-on agent is desired.** The official `letta` repository now directs active development to **letta-code**; the previous V1 API server is retired on `archive`. Do not design from old server tutorials. [Official migration notice](https://github.com/letta-ai/letta)

Current Letta Code offers memory blocks, Git-tracked MemFS, conversation search, skills, reflection and schedules. The npm/Node runtime can operate locally; **Cloud is the default**, with an explicit local backend option. Model credentials remain separate; reflection and constantly included memory add token use, while compact memory may save repeated history reading. Cloud stores agent memory/conversations. [v0.34.24 README](https://raw.githubusercontent.com/letta-ai/letta-code/v0.34.24/README.md)

It could replace orchestration rather than merely bolt on memory, but importing its agent state alongside existing clients would increase integration burden. It is not a canonical Jira/GitHub ledger. **Apache-2.0 on current main; v0.34.24 released October 25** includes recovery/listener fixes, indicating rapid change. [License](https://raw.githubusercontent.com/letta-ai/letta-code/main/LICENSE), [release](https://github.com/letta-ai/letta-code/releases/tag/v0.34.24)

### QMD versus PageIndex: choose for the corpus

**QMD: first evaluation.** Local BM25 + embeddings + query expansion/reranking suits Markdown notes and paraphrased work questions. Its CLI/MCP can sit behind the existing retrieval interface. Node/SQLite and local GGUF models mean downloads, disk, RAM and indexing time, rather than mandatory per-query provider charges; snippets still enter the answering client's context. It retrieves documents, not commitments. **MIT; v2.8.3, August 16** fixes unapproved project-local update commands. Keep configuration trusted and the evaluation read-only. [Tagged README](https://raw.githubusercontent.com/tobi/qmd/v2.8.3/README.md), [license](https://raw.githubusercontent.com/tobi/qmd/v2.8.3/LICENSE), [release](https://github.com/tobi/qmd/releases/tag/v2.8.3)

**PageIndex: skip now, revisit for long structured PDFs.** Its tree-guided document retrieval targets manuals/reports rather than many short mutable events. Current main separates structural extraction from model-based refinement and model-guided querying. Python SDK plus configured models is the minimum; local indexing does not imply local inference. Upfront indexing can be reused, but each reasoning traversal still incurs model work. It supplies neither personal memory nor workflow state. **MIT on inspected main**; no latest package version was established here. Its financial-document benchmark is vendor-authored and domain-specific, not evidence of superiority on project updates. [Current README](https://raw.githubusercontent.com/VectifyAI/PageIndex/main/README.md), [license](https://raw.githubusercontent.com/VectifyAI/PageIndex/main/LICENSE)

### Tencent TeamAI CLI: team procedures, not universal personal memory

**Evaluate later for shared engineering practices.** Verified identity: **Tencent/teamai-cli**, public package `teamai-cli`; current main license is **MIT**. Latest stable observed **v0.27.0 October 9**, with **v0.28.0-beta.8 October 21**. Main documentation may exceed stable features. Node 20+, Git/team repository and supported coding clients are the basic operating footprint. [Repository](https://github.com/Tencent/teamai-cli), [license](https://github.com/Tencent/teamai-cli/blob/main/LICENSE), [releases](https://github.com/Tencent/teamai-cli/releases)

It distributes skills/rules/MCP configuration and captures/retrieves team learnings. The current guide describes corrections prompting knowledge contribution, reviewed learning promotion, and codebase evidence. Its confidence score measures activity, **not factual correctness**. This is useful procedural reuse, not model-weight training or automatic stakeholder management. Extraction/judging uses configured AI tooling; injected rules consume context. Git sharing, hooks and usage reporting require a scope/privacy review before rollout. It does not solve personal unanswered messages or cross-team ticket reconciliation. [Knowledge guide](https://github.com/Tencent/teamai-cli/blob/main/docs/guide/knowledge.md), [reporting guide](https://github.com/Tencent/teamai-cli/blob/main/docs/guide/reporting.md)

### One durable-workflow option: DBOS

**Evaluate later**, when crashes and retries—not recall—become the bottleneck. Its TypeScript library checkpoints workflow steps in PostgreSQL and resumes execution. No LLM is required; costs are database operations, storage and worker hosting. It would add PostgreSQL to today's SQLite deployment. External effects still need idempotency/reconciliation: a restart must not resend a stakeholder reply. [Repository](https://github.com/dbos-inc/dbos-transact-ts)

The core package declares **MIT**. **v5.3, October 15** separates Conductor/Cloud integration into an Enterprise package; don't conflate OSS durability with hosted control-plane features. This is a stronger reason to reuse than inventing a distributed workflow engine, but premature for a local approval queue. [Package](https://github.com/dbos-inc/dbos-transact-ts/blob/main/package.json), [release](https://github.com/dbos-inc/dbos-transact-ts/releases/tag/v5.3)

## What the evidence suggests is emerging

The useful direction is **layered, auditable experience**, not a single magical memory database: compiled knowledge, temporal evidence, reusable procedures, bounded retrieval and durable execution. That is an inference from the contrasting systems above.

A particularly relevant research development is **LongMemEval-V2**: it tests static recall, dynamic state, workflows, environment pitfalls and premise awareness using agent trajectories. Its AgentRunbook variants separate observations, events and strategy notes or use files plus an evidence-gathering agent. The May 2026 paper explicitly acknowledges latency tradeoffs and is marked work-in-progress. It supports testing procedural knowledge, not declaring any listed product the winner. [Paper](https://arxiv.org/abs/2605.12493), [official repository](https://github.com/xiaowu0162/LongMemEval-V2)

No independent apples-to-apples evaluation of all these current releases was established. Mem0's published benchmark repository identifies managed Platform V3 results; those cannot be transferred to local OSS configurations. Reader model, judge, corpus version, context budget and ingestion cost all change comparisons. [Benchmark provenance](https://github.com/mem0ai/memory-benchmarks)

## Small experiments before any adoption

Use synthetic fixtures only; establish the baseline before installing anything. No paid benchmark runs are needed initially.

1. **Retrieval:** author 60 source events across three fictional projects and 30 held-out questions. Compare SQLite/FTS with QMD later, using identical evidence-token caps. Include aliases, exact ticket IDs, paraphrases and unanswerable questions. Measure evidence recall, wrong-project leakage, latency and index maintenance—not just fluent answers.
2. **Memory correctness:** replay changed owners, reversed decisions, duplicate events and source deletion. Compare explicit claims/SQL history with later Mem0 or Graphiti prototypes. Require correct current versus historical answers, provenance and abstention. Mock model outputs test plumbing only, not extraction quality.
3. **Procedure reuse:** create ten reviewed runbooks with exceptions and ten tempting wrong-team matches. Compare ordinary scoped Markdown with TeamAI-style recall. Reject promotion based solely on use counts.
4. **Work completion:** simulate a crash after remote acceptance but before local receipt. Require reconciliation without duplicate ticket comments/messages. A draft must remain distinct from an approved action and a verified receipt.

Promote a component only when it improves a held-out failure class without weakening isolation, deletion or approval guarantees. The first product increment should make “what needs my attention, why, and what can I safely approve?” reliable. A larger memory backend comes afterward.
