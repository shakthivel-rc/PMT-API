# 3. AI integration strategy

This document covers four AI features: thread summarisation, story-point estimation,
auto-assignment and ticket generation. Summarisation ships in Phase 1 and is described as
built. The other three are designed on the same infrastructure, so each one is a new job
kind and a new prompt rather than a new system.

## 3.1 Principles

1. **Never on the request path.** An API request that needs a model inserts a job and returns
   202. A separate worker pool calls the model. This is the requirement that long-running
   inference must not block the API, and three things enforce it:
   - The API process never imports the model client.
   - `tests/unit/test_ai_isolation.py` fails if it does.
   - The API process is never given `ANTHROPIC_API_KEY`.
2. **AI suggests, people decide.** Estimates, assignees and drafted tickets are shown as
   suggestions with the evidence behind them. The person accepts, edits or rejects each
   one, and that decision is recorded in `ai_suggestions` and becomes evaluation data.
   Automatic application (for example, auto-assign) is a workspace setting that is off by
   default.
3. **Prompts are code.** Each prompt is versioned (`ai/prompts.PROMPT_VERSION`), reviewed,
   tested and stored on every job it answers. A change to the prompt invalidates cached
   answers, and a quality regression can be traced to the prompt change that caused it.
4. **Output is structured.** Every feature asks for JSON that matches a schema
   (`output_config.format`). The worker validates it against a Pydantic model before storing
   it. Nothing downstream parses prose.
5. **User text is data.** Issue text is fenced and the model is told that the fence contains
   material to process, not instructions. Output is rendered as plain text, never as HTML or
   Markdown. The worst a prompt injection can do is make one suggestion wrong, and a person
   reviews every suggestion.
6. **Tenant-scoped context.** Retrieval only returns rows from the requester's workspace
   (enforced by RLS and a `WHERE workspace_id` filter) and only what the requester can see.
   A summary is visible to every member, so it is built only from what every member can read.

## 3.2 Infrastructure

```
 API ──insert──▶ ai_jobs (queue)          ai-worker pool (KEDA: scale on queue depth & age)
  ▲  202 + job        │                    ┌──────────────────────────────────────────────┐
  │                   └──claim (SKIP LOCKED,│ 1. read context (short txn, RLS on the job's │
  │                        lease + fencing) │    workspace)                                 │
  │ poll / WS push                          │ 2. LLM gateway: budget check → prompt from    │
  │                                         │    registry → Claude (fallbacks, retries)     │
  └──────────── result ◀────────────────────│ 3. validate → write result (fenced) → usage   │
                                            └──────────────────────────────────────────────┘
       outbox events ──▶ embedding worker ──▶ Voyage embeddings ──▶ embeddings (pgvector)
```

| Concern | Phase 1 (built) | Later |
|---|---|---|
| Queue | The `ai_jobs` table. Claims use `FOR UPDATE SKIP LOCKED`, a 15-minute lease, a fencing token (`attempts`), and backoff of 30 s, 120 s and 300 s. | A `priority` lane (interactive before background). Redis Streams when claim throughput needs it: at a sustained claim rate of about 50/s the polling cost on Postgres becomes significant. |
| Workers | `workers/ai_worker.py`, a separate Deployment (compose service `ai-worker`). Stops gracefully on SIGTERM and holds no database connection during a model call. | KEDA autoscaling on queue depth and the age of the oldest job. |
| Model access | `ai/client.py` calls the official Anthropic SDK with `fallbacks="default"`, adaptive thinking, structured output, a 180 s timeout and 2 retries. | Grows into an **LLM gateway** module: per-workspace token budgets (`ai_usage`), per-workspace concurrency limits, model routing per feature, and PII redaction before sending. |
| Prompts | Versioned Python constants with unit tests (`tests/unit/test_ai_prompts.py`). | A prompt registry (a table plus files), so prompts can be A/B tested per feature. |
| Caching | Input-hash reuse. If a thread has not changed, the last summary is returned with `cached: true` and no model call. | Anthropic prompt caching for large stable prefixes, such as retrieved context and team conventions. |
| Cost control | `AI_ENABLED` (off by default). The summary route needs the `member` role. | Monthly token budgets per workspace. The Batch API (half price) for backfills such as bulk embeddings and re-estimation. Effort tuned per feature from measurements. |
| Observability | One log line per job with tokens, model, request ID and attempt. | Cost and latency per feature and per workspace, failure rate, and the acceptance rate of suggestions. |

### Model choice

- **Generation.** The default is Claude Opus 5 (`claude-opus-5`), set by `AI_MODEL`, with
  effort set by `AI_EFFORT` (default `high`). Server-side fallbacks (`fallbacks: "default"`)
  re-run a request that a safety classifier declines on a recommended fallback model, within
  the same call. This helps for security-flavoured engineering text that occasionally trips a
  classifier. Summaries are a good candidate for `medium` effort, but that is decided by
  measuring against the evaluation set (§3.6), not by assumption.
- **Embeddings.** Anthropic does not offer an embeddings model, so embeddings come from a
  dedicated provider. The recommended default is Voyage AI (for example `voyage-3.5` for
  prose, 1024 dimensions, with a code-specific model benchmarked for PR and commit text).
  Vectors are stored per model (`embeddings.model`), so switching models means a backfill
  alongside the existing vectors followed by a flag flip.
- **No LangChain.** Each feature is one structured call plus our own retrieval. The official
  SDK exposes new API features, such as fallbacks, structured outputs, prompt caching and
  batches, the day they ship. An abstraction layer would add dependencies and delay those
  features without doing work we need. If a feature becomes genuinely agentic (multi-step
  tool use), use the SDK's tool runner before adopting a framework.

## 3.3 Smart summarisation (Phase 1, built)

**Scope.** An issue's title, description, comments (oldest first, with authors and times) and
linked pull requests (title and state).

**Output.** A `ThreadSummary` containing:
- `tldr`: 2–3 sentences
- `decisions[]`
- `open_questions[]`
- `action_items[]`: each with `{owner, text}`, where owner is the name as written in the
  thread, or null

**Flow.**
1. `POST …/issues/{id}/summary` renders the thread and hashes it together with
   `PROMPT_VERSION`.
2. The route then does one of three things:
   - If a succeeded job exists for that hash, it returns that job with `cached: true` and
     makes no model call.
   - If a job for this issue is queued or running, it returns that job, so the model is not
     paid for twice.
   - Otherwise it enqueues a new job.
3. The worker renders the thread again (so the hash covers exactly what the model sees), calls
   Claude, validates the result against `ThreadSummary`, and writes it only if the job's
   attempt number still matches its own (the fencing token).
4. The SPA polls `GET …/ai-jobs/{id}` every 1.5 s, for at most 2 minutes. From Phase 2 it is
   pushed an `ai_ready` notification over WebSocket instead.

**Limits.**
- A thread over 400,000 characters (about 100k tokens) is refused with a message. It is not
  silently truncated, because a summary of the first half of an argument reads as the
  conclusion of the whole argument.
- A model refusal or a truncated answer fails the job with a readable message. Rate limits,
  5xx errors and connection errors retry with backoff, up to `AI_MAX_ATTEMPTS`.

**Next.**
- *PR discussion summaries*: fetch review comments through the GitHub App installation token
  and include them in the same thread.
- *Epic scope summaries*: map-reduce. Each child's summary is cached separately under its own
  input hash, and the epic's summary is generated from its children's summaries. Changing one
  story only re-summarises that story and the epic.

## 3.4 Intelligent auto-assignment

This is a **ranking problem, with the model used only where language is involved**. Most of
the signal is structured data, which is cheaper, deterministic and explainable when computed
directly. For a new or re-triaged issue:

| Signal | Source | How it is computed |
|---|---|---|
| **Expertise: code** | `file_touches` (from push webhooks), and the paths touched by PRs on similar resolved issues | Claude extracts likely components and paths from the issue text (structured output: `{components[], paths[]}`). Each candidate is scored by recency-weighted commits under those path prefixes. This answers "who most often changes the affected code". |
| **Expertise: history** | `embeddings` kNN over resolved issues in the workspace | Who resolved the 20 most similar issues, weighted by similarity and recency. |
| **Availability** | Open story points or hours against `capacity` for the sprint, and `time_off` | A candidate on PTO during the issue's window is excluded. Load ratio is open work divided by capacity. |
| **Fit** | Team membership, the space's team, skills labels | A hard filter, then a small boost. |
| **Fairness** | Assignments in the last N days | A penalty to avoid always sending work to the same expert. It can be switched off per space. |

The score is `expertise_code^a · expertise_history^b · (1 − load)^c · fairness`, with
weights tuned per workspace from accept/reject data in `ai_suggestions`.

The output is the top 3 candidates, each with a reason written from the evidence: "changed
`payments/refunds/` 14 times in 60 days; resolved PAY-88 and PAY-101, which are similar;
at 60% capacity this sprint". The model writes that sentence from the structured evidence,
so it cannot invent evidence. The workspace setting `auto_assign` controls behaviour:

| Setting | Behaviour |
|---|---|
| `off` | No suggestions |
| `suggest` (the default once enabled) | Show the ranked candidates |
| `apply` | Assign automatically, but only above a confidence threshold, and record it as an `ai` actor event |

## 3.5 Story-point estimation

1. **Retrieve.** Find the k nearest *completed* issues in the same space or team by embedding
   similarity of title and description. For each one, fetch its final points, cycle time,
   type and the number of PRs and files changed.
2. **Reason.** Claude sees the new issue and those neighbours, including their outcomes, plus
   the team's scale (Fibonacci, t-shirt sizes or hours from `spaces.settings`). It returns
   structured output: `{estimate, confidence, comparable_issue_ids[], risks[]}`, where risks
   include unknowns in the text such as "no acceptance criteria", "touches payments" or "new
   integration".
3. **Calibrate.** Once a team has about 200 or more completed estimated issues, a small
   gradient-boosted regressor is trained per team. Its features are neighbour points, text
   length, the presence of acceptance criteria, component risk, and the assignee's historical
   bias. It *adjusts* the model's estimate, and the model still writes the rationale. Measure
   mean absolute error against the team's own final estimates, and only show the suggestion
   when it beats the team's median-points baseline.
4. **Velocity feedback.** Sprint planning shows suggested totals against the team's rolling
   velocity, from `sprints.completed_points`, so over-commitment is visible before the sprint
   starts.

## 3.6 Ticket generation from conversations and transcripts

- **Input.** Pasted text or an uploaded meeting transcript, plus the target space. The
  space's epics are included, so drafts can be attached to an existing epic.
- **Output schema.** A list of drafts, each with:
  - `{type, title, user_story ("As a … I want … so that …")}`
  - `acceptance_criteria[]` in Given/When/Then form
  - `suggested_parent_epic_id`
  - `open_questions[]`
  - `source_quote`: the transcript lines the draft came from, as evidence
- **Deduplication.** Before the drafts are shown, each is compared by embedding similarity
  against open issues in the space. A likely duplicate is flagged with a link instead of being
  offered for creation.
- **Review.** A preview screen lets the person edit, discard, and then bulk-create. Nothing is
  created without a person confirming it.
- **Long transcripts.** Transcripts beyond one model call are split on speaker turns with
  overlap. Drafts are extracted from each chunk and then merged in a final call that removes
  duplicates across chunks.

## 3.7 RAG architecture (context for estimation, assignment and generation)

```
 outbox (issue/comment/doc/PR changed) ─▶ embedding worker
      chunk (title+description; each comment; doc sections ~800 tokens, 15% overlap)
      content_hash unchanged? skip : embed (Voyage) ─▶ embeddings (pgvector HNSW)

 query ─▶ hybrid retrieval, always WHERE workspace_id = :w (and RLS)
            ├─ vector kNN (embeddings, cosine; iterative scan for filtered recall)
            └─ lexical BM25 (OpenSearch; Postgres FTS before Phase 2)
         ─▶ reciprocal rank fusion ─▶ permission filter (the requester can see it)
         ─▶ top-k context with ids ─▶ prompt (context first, cacheable) ─▶ cited output
```

- **Permission-aware.** Retrieval runs as the requesting user, with RLS for the tenant and
  space-level visibility filters, so the model never sees what the requester could not open.
- **Cited.** Every suggestion carries the IDs of the issues and PRs it relied on, and the UI
  links to them. An answer with no citation is shown as low-confidence.
- **Fresh.** Embeddings are updated from the outbox within seconds of a change, and
  `content_hash` means unchanged text is never paid for twice.

## 3.8 Evaluation and quality

- **A golden set per feature.** It is built from real, consented threads and resolved issues,
  and grows from `ai_suggestions` decisions. Summaries are graded against a rubric (faithful,
  complete on decisions and blockers, no invented owners) by a pinned model-graded judge,
  spot-checked by people. Estimates are measured by mean absolute error against final points.
  Assignment is measured by acceptance rate and hit@3.
- **Regression gate.** Any change to `PROMPT_VERSION`, the model or the effort level runs the
  evaluation set in CI (with a budget cap). A drop beyond a threshold blocks the merge.
- **Online.** Track acceptance and edit rates per feature and per workspace. Falling
  acceptance is the earliest sign of drift.

## 3.9 Data governance

- **Per-workspace opt-in.** `workspaces.settings.ai.enabled` sits alongside the
  deployment-wide `AI_ENABLED`. Enterprise workspaces can require zero-data-retention
  arrangements with the provider.
- **No training on customer data.** API traffic is not used for model training under the
  provider's commercial terms. Embeddings and prompts are stored in the customer's cell and
  deleted with the workspace.
- **Minimise.** Send the fields a feature needs. Redact secrets that look like keys or tokens
  (`core/redact.py` patterns) from issue text before sending.

## 3.10 Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Provider 429 or 5xx | Job delayed | SDK retries, then job-level backoff (30 s, 120 s, 300 s), up to `AI_MAX_ATTEMPTS` |
| Safety classifier declines | Would fail the job | Server-side `fallbacks: "default"`. If the whole chain refuses, the job fails with "the model declined". |
| Worker crash during a call | Job stuck as `running` | The lease expires after 15 min and another worker reclaims the job. The fencing token discards a late write from the old worker. |
| Truncated or malformed output | Unusable answer | `max_tokens` stop means fail. Schema validation failure means retry. |
| Budget exhausted | Cost overrun | The gateway refuses with 429 and a message. Interactive requests take priority over background ones. |
| AI disabled or unavailable | Feature missing | The API returns 503 with a message. Core tracking is unaffected. |
