# 1. System architecture

This document describes the target architecture of PMT: a multi-tenant project-management
platform for software teams. It covers the components and how they are wired together, where
state lives, how each piece stays available through a failure, and how tenants are kept
apart. Phase 1 builds a subset of it (see [06-phase-1-plan.md](06-phase-1-plan.md)), and
nothing Phase 1 builds has to be thrown away to reach the rest.

## 1.1 What the architecture has to deliver

| Requirement | What it means in practice | How it is met |
|---|---|---|
| High availability | No single point of failure. Losing one availability zone (AZ) does not take the product down. | Everything runs as at least 2 replicas across 3 AZs. Postgres uses a synchronous standby with automated failover. Services are stateless. |
| Multi-tenancy | One customer can never read another customer's data, even when there is a bug. | `workspace_id` is on every row. The application checks membership, and PostgreSQL row-level security (RLS) enforces the same boundary underneath it. Cache keys, queues and AI budgets are all scoped by workspace. |
| Reads under 200 ms (p95) | Opening a board or list is one short round trip. | Denormalised card projections. Covering indexes. Read replicas. A Redis cache for hot boards, added once measurements show it is needed. |
| Horizontal scalability | Add pods, not bigger machines. | Stateless API and workers on Kubernetes. Autoscaling on CPU for the API and on queue depth for workers. |
| AI never blocks the API | A slow model call cannot use up request capacity. | Every model call goes through a queue and runs in a separate worker pool. The API process does not have the model client installed, and a test checks this. |
| Real-time collaboration | Boards update live. Descriptions and wiki pages support multiplayer editing. | A WebSocket gateway fed by the event stream. Document editing uses Yjs CRDTs. |

## 1.2 Component diagram

```
                           ┌───────────────────────────────────────────────────────────┐
  Browsers (React SPA)     │  CDN + WAF (static assets, TLS, bot/DDoS protection)      │
  IDE / CLI (Phase 3)  ───▶│  Global load balancer — health-checked, multi-AZ          │
  GitHub/GitLab/Bitbucket  └──────────────┬──────────────────────────┬─────────────────┘
  webhooks                               HTTPS                      WSS
                                          │                          │
┌─────────────────────────── Kubernetes cluster (3 AZs) ─────────────┼────────────────────┐
│                                         ▼                          ▼                    │
│  ┌────────────────┐   ┌──────────────────────────┐   ┌──────────────────────────────┐  │
│  │ web (nginx)    │   │ api  (FastAPI, N pods)    │   │ realtime gateway (Node, N)   │  │
│  │ SPA bundle     │   │ REST, stateless, HPA      │   │ WebSocket fan-out, presence, │  │
│  └────────────────┘   │ auth · tenancy · RBAC     │   │ Yjs/Hocuspocus doc sync      │  │
│                       └──┬───────┬──────┬────────┘   └──────┬──────────────┬─────────┘  │
│                          │       │      │ enqueue           │ subscribe    │ persist    │
│              read/write  │       │      ▼                   │              │ doc state  │
│                          │       │  ┌───────────────────────┴──────┐       │            │
│                          │       │  │ queues  (Phase 1: Postgres    │       │            │
│                          │       │  │ table; Phase 2: Redis Streams)│       │            │
│                          │       │  └──┬─────────┬──────────┬───────┘       │            │
│                          │       │     ▼         ▼          ▼               │            │
│                          │       │ ┌────────┐ ┌─────────┐ ┌────────────┐   │            │
│                          │       │ │ai-worker│ │automation│ │integration │   │            │
│                          │       │ │ pool    │ │ engine   │ │ sync (VCS, │   │            │
│                          │       │ │ (KEDA)  │ │          │ │ CI/CD)     │   │            │
│                          │       │ └───┬─────┘ └────┬─────┘ └─────┬──────┘   │            │
│                          │       │     │ LLM gateway│             │          │            │
│                          │       │     ▼ (budgets, │             │          │            │
│                          │       │  Claude API     │             │          │            │
│                          │       │  Voyage embeds  │             │          │            │
│                          ▼       ▼                 ▼             ▼          ▼            │
│  ┌────────────────────────────────────────────────────────────────────────────────────┐ │
│  │ PgBouncer ──▶ PostgreSQL 16  primary (AZ-a) ══sync══ standby (AZ-b) ── async (AZ-c)│ │
│  │               RLS · JSONB · pgvector · outbox · partitioned event log              │ │
│  └───────────────┬───────────────────────────────────────────────┬────────────────────┘ │
│                  │ outbox relay / CDC                            │ WAL archive          │
│                  ▼                                               ▼                      │
│  ┌──────────────────────────┐  ┌────────────────────────┐  ┌──────────────────────────┐ │
│  │ Redis (3-node, Sentinel/ │  │ OpenSearch (3 masters, │  │ Object storage (S3/GCS): │ │
│  │ cluster): cache, pub/sub,│  │ data nodes per AZ):    │  │ attachments, exports,    │ │
│  │ rate limits, presence    │  │ search + PQL full text │  │ base backups + WAL (PITR)│ │
│  └──────────────────────────┘  └────────────────────────┘  └──────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

The published architecture artifact renders the same diagram as a figure.

### Components

| Component | Responsibility | State | Scales on |
|---|---|---|---|
| **web** | Serves the SPA bundle with a strict CSP (already in `pmt-app/nginx.conf`). | none | replicas behind the CDN |
| **api** | REST API: authentication, tenancy, RBAC, issues, views, PQL. It writes to Postgres and to the outbox in one transaction. | none | CPU / p95 latency (HPA) |
| **realtime gateway** | Authenticates WebSockets with short-lived tickets from the API. Subscribes to workspace, board and issue channels. Hosts Yjs documents through Hocuspocus. | in-memory Yjs docs, persisted to Postgres | connection count |
| **ai-worker** | Claims AI jobs, reads their context, calls the model with no database connection held, and writes the result back. | none | queue depth (KEDA) |
| **automation engine** | Consumes domain events, evaluates workspace rules ("when a PR is merged, move to Done and log time"), and runs the actions idempotently. | none | queue depth |
| **integration sync** | Processes VCS and CI webhooks and runs outbound sync (status checks, branch creation). | none | queue depth |
| **outbox relay** | Moves committed `outbox` rows to Redis Streams, and later Kafka, for fan-out to realtime, search and automation. | cursor in Postgres | one active instance per shard, chosen by leader election |
| **PostgreSQL** | The system of record. | all durable state | vertical scaling plus read replicas, then cells |
| **Redis** | Cache, pub/sub, presence, distributed rate limiting, and later the queue broker. | ephemeral | cluster shards |
| **OpenSearch** | Full-text search and PQL text predicates across issues, comments and docs. | derived, can be rebuilt | shards |

## 1.3 Request flows

**Read path: open a board.** `GET /workspaces/{w}/board?list_id=`

1. The load balancer routes the request to any API pod. JWT middleware checks the session row by
   an indexed lookup on its SHA-256 hash.
2. `workspace_access` resolves membership with one indexed query. A non-member gets a 404.
3. From Phase 2, the API looks for the board in Redis under the key
   `board:{w}:{scope}:{generation}` and returns it on a hit.
4. On a miss the board is built from 5 indexed queries, whatever its size: statuses, issues
   (on `ix_issues_workspace_list_status_rank`), space keys, assignees and PR states. The
   queries can be routed to a replica, with a read-your-writes guard (§1.6).

**Write path: move a card.** `POST …/issues/{id}/move`

1. The API computes a new fractional rank from the anchor card, re-reading its neighbour from
   the database, and updates that one row. It writes `issue_events` and, from Phase 2, an
   `outbox` row in the same transaction.
2. The outbox relay publishes `issue.moved`. The realtime gateway pushes it to every client
   subscribed to the board, the cache generation for that board is bumped, and the search
   indexer updates the document.
3. Clients apply the event to their normalised store, so every open view updates without a refetch.

**AI path: summarise a thread.** Covered in [03-ai-strategy.md](03-ai-strategy.md).

1. The API reads the thread, computes an input hash, and returns the cached result if one
   exists for that hash.
2. Otherwise it inserts an `ai_jobs` row and returns 202.
3. A worker claims the job with `FOR UPDATE SKIP LOCKED` and a lease, calls Claude with no
   database connection held, and writes the result under a fencing token.
4. The client polls, or from Phase 2 receives a push.

**Webhook path: GitHub.**

1. The signature is verified over the raw body before any parsing. A delivery ID that has
   already been processed is a no-op.
2. The handler links the PR to the issues named in its branch, title or body. When a linked
   PR is merged it moves those issues to Done.
3. All of this happens in one transaction. From Phase 2 the handler only verifies and
   enqueues, and a worker does the processing, so a burst of deliveries is absorbed by the
   queue instead of by API pods.

## 1.4 High availability: no single point of failure

| Layer | Redundancy | Failure handling | RTO / RPO target |
|---|---|---|---|
| Edge | CDN and global load balancer from a managed provider | Anycast, with health checks per region | provider SLA |
| api / web / gateway / workers | At least 2 replicas per AZ, with pod anti-affinity and PodDisruptionBudgets | Liveness probe on `/health/live` (touches nothing) and readiness probe on `/health/ready`. Rolling deploys with surge. | RTO under 30 s (pod replacement) |
| PostgreSQL | Patroni or CloudNativePG: a primary, a **synchronous** standby in another AZ, and an async replica in a third AZ. PgBouncer in front. | Automated failover in 30 s or less. Clients reconnect through the PgBouncer or service VIP. `pool_pre_ping` (already in `db/session.py`) discards dead connections. | RTO under 60 s, **RPO 0** within the region (synchronous commit) |
| Backups | Continuous WAL archiving plus daily base backups to object storage in another region | Point-in-time recovery (PITR) to any second in the retention window, restore-tested weekly | RPO under 5 min for regional disaster recovery |
| Redis | 3-node Sentinel, or managed cluster mode with replicas | Automatic promotion. The **cache is never the source of truth**: a Redis outage degrades latency, never correctness. | RPO not applicable (derived data) |
| OpenSearch | 3 dedicated masters, replicas in every AZ | The index can be rebuilt from Postgres. PQL falls back to Postgres full-text search, more slowly. | RPO not applicable (derived data) |
| Queue / AI | Jobs are rows, or stream entries with consumer groups, and are claimed under a lease | A crashed worker's job is re-claimed after its lease expires. Handlers are idempotent. Retries are bounded with backoff. | Jobs are never lost (at-least-once delivery) |
| LLM provider | Server-side model fallbacks, SDK retries, and a job-level retry with backoff | A provider outage fails AI features softly (a queued job plus a message) and never affects core tracking | not applicable |
| Region | Phase 3: a warm standby region with async Postgres replication and infrastructure as code | Promotion from a runbook, with DNS failover | RTO under 1 h, RPO under 1 min |

Two principles lie behind the table:

- **Everything outside Postgres is derived or disposable.** Redis, OpenSearch, queues,
  realtime state and caches can all be rebuilt from the database. Only one component
  therefore needs RPO 0.
- **Degrade by capability, not by product.** When AI, search, realtime or VCS sync fails,
  that one feature becomes unavailable and core tracking keeps working. Phase 1 already
  follows this rule: with AI disabled, the summary endpoint answers 503 and nothing else changes.

## 1.5 Multi-tenancy

**Model: pooled tenancy with row-level isolation, grouped into cells.**

- **Tenant key.** `workspace_id` is on every tenant-owned row, including child rows such as
  comments, events, links and jobs. This avoids a join to find a row's tenant, and it makes an
  RLS predicate a plain indexed equality check.
- **Enforcement layer 1: the application.** `dependencies/workspace.workspace_access` resolves
  membership once per request and returns a `WorkspaceAccess`. Controllers take the workspace
  ID only from that object, and every service query filters on it, including lookups by
  primary key. A non-member receives a 404 that cannot be distinguished from a workspace that
  does not exist.
- **Enforcement layer 2: the database** (after the PostgreSQL cutover). Every tenant table
  has `ENABLE ROW LEVEL SECURITY` and a policy of
  `USING (workspace_id = current_setting('app.workspace_id')::uuid)`. The API sets the value
  per transaction with `SET LOCAL`, as the application role, which does not have
  `BYPASSRLS`. A query that forgets its filter then returns nothing instead of another
  tenant's rows. The DDL is in [schema.sql](schema.sql).
- **Everything else is keyed by tenant too.** Cache keys (`board:{workspace}:…`), WebSocket
  channels, search documents (a `workspace_id` filter on every query, plus index routing),
  object-storage prefixes, AI jobs and embeddings (RLS and `WHERE workspace_id =`), rate
  limits and AI token budgets.
- **Noisy neighbours.** There are per-workspace request rate limits (Redis token bucket),
  per-workspace AI concurrency and monthly token budgets, statement timeouts, and board
  pagination above a column-size threshold.
- **Cells, for scale and blast radius.** A cell is one full stack: a Postgres cluster, Redis,
  OpenSearch and workers. A small routing service maps `workspace_id` to a cell. The first
  cell is the whole deployment. Adding cells caps the size of each database, so no
  application-level sharding is needed, and it limits an incident to the tenants in one
  cell. An enterprise customer who needs isolation gets a **dedicated cell**: the same code
  with a different route.

## 1.6 Consistency choices

- **Strong consistency inside one aggregate.** An issue's own fields, its status and its rank
  are updated in single-row transactions. Concurrent edits are caught with optimistic
  concurrency (`issues.version`, and a 409 on a stale version). Board ranks use fractional
  indexing, so two concurrent moves touch different rows and never renumber a column.
- **Eventual consistency across projections.** Search, caches, realtime pushes, analytics and
  embeddings all lag the database by the delay of the outbox relay, typically under a second.
- **Read-your-writes on replicas.** After a write, the API stores the commit LSN in the
  session. For the next few seconds, reads go to a replica only if it has replayed past that
  LSN, and to the primary otherwise.

## 1.7 Performance budget: a p95 board read in 200 ms

| Stage | Budget | How |
|---|---|---|
| Edge and TLS | 20 ms | CDN and keep-alive |
| Authentication and tenancy | 10 ms | 2 indexed point reads. These can be cached for 30 s in Redis if measurement shows it is needed. |
| Board queries | 60 ms | 5 index-backed queries with no N+1. Covering index on `(workspace_id, list_id, status_id, rank)`. |
| Serialisation | 20 ms | Lean `IssueCard` projection. Descriptions are never included in board payloads. |
| Headroom | 90 ms | GC, network variance, cold cache |

Past about 2,000 cards on one board, columns are paginated with a keyset cursor on
`(rank, id)`, and the SPA virtualises the columns.

## 1.8 Security architecture (summary)

- **Identity.** Hardened login is inherited from the base: rotating refresh tokens,
  server-side sessions stored as hashes, lockout, and a password policy. SSO through OIDC and
  SAML, plus SCIM provisioning, are enterprise features for Phase 3.
- **Authorisation, in two layers.** Platform RBAC (`core/permissions.py`) controls who can
  administer the product. Workspace roles (guest, member, admin, owner) control what someone
  can do inside a tenant. Later, a space can restrict visibility with space-level membership.
- **Secrets.** These come from environment variables through External Secrets or Vault. The
  webhook secret and the AI key live only in the processes that need them. Stored
  third-party tokens, such as a GitHub App installation token, are encrypted with a key that
  is separate from `SECRET_KEY` (CLAUDE.md §7 rule 3).
- **Data protection.** TLS everywhere. Storage is encrypted at rest, with customer-managed
  keys for enterprise cells. Every log line passes through the redactor, which now also
  recognises Anthropic keys.
- **Audit.** `activity_logs` records administrative actions and `issue_events` records every
  change to an issue. Both are append-only.

## 1.9 Observability

- **Traces.** OpenTelemetry traces cover the API, workers and gateway. `X-Request-ID`
  (already present) becomes the trace correlation ID.
- **Metrics.** RED metrics (rate, errors, duration) are recorded per route and queue depth
  and age per queue. For AI: tokens, cost, latency and failure rate per feature and per
  workspace.
- **SLOs with error budgets.** API availability 99.95%. Board read p95 under 200 ms. AI summary
  p95 under 30 s from request to result. Webhook-to-link p95 under 5 s.
- **Structured JSON logs** with secret redaction (`core/logging.py`), shipped to Loki or an
  equivalent.
