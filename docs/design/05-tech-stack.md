# 5. Tech stack

## 5.1 The starting point

PMT does not start from zero. `base-project-api` and `base-project-app`, copied here as
`pmt-api` and `pmt-app`, already provide:

- **Hardened identity.** Rotating refresh tokens, server-side sessions stored as hashes,
  lockout, a password policy, and OTP, invite and reset flows.
- **RBAC** built on a permission slug catalogue.
- **An audit log.**
- **Hardened edges.** Secret redaction in every log line, rate limiting, security headers, CORS
  and body-size limits.
- **Build gates that already enforce discipline.** A lint ledger, scoped mypy, coverage and
  bundle-size ratchets, an OpenAPI contract digest, a route-inventory test (every route
  answers 401 without a token unless it is listed as public), and a test that no blocking
  database call runs on the event loop.
- **Delivery tooling.** Docker, CI and a one-command setup.

Throwing that away to switch languages would cost months and discard proven security work.
The recommendation is to **keep the API and SPA stack, change the database to PostgreSQL, and
add the pieces the product needs.**

## 5.2 Recommended stack

| Layer | Choice | Why | Alternatives considered |
|---|---|---|---|
| **SPA** | React 19, TypeScript, Vite, Tailwind v4, shadcn/ui (existing) | Already built, typed, themed and gated. React has the richest ecosystem for board, Gantt and editor components. | Svelte/Solid: smaller bundles, but the component ecosystem is thinner and the team would retrain |
| Client state | Redux Toolkit (existing) with **RTK Query** entity adapters for the normalised issue store | Already a dependency. Normalised entities are exactly what multi-view regrouping and realtime patches need (04 §4.3). | TanStack Query: excellent, but would be a second state library |
| Drag and drop | Native HTML5 in Phase 1, with a keyboard "Move to…" menu. `@dnd-kit` once columns are virtualised. | No new dependency until one is needed. dnd-kit handles virtualised lists and touch. | react-beautiful-dnd (unmaintained) |
| Editor | TipTap (ProseMirror) with Yjs | The standard pair for collaborative rich text. Stores structured JSON, not HTML. | Lexical with Yjs (good, but a smaller plugin ecosystem) |
| Charts and Gantt | visx / Recharts for reports. A custom SVG Gantt with virtualisation. | Gantt requirements (dependencies, drag-resize, zoom) outgrow off-the-shelf free components. | dhtmlxGantt / Bryntum (commercial licences) |
| **API** | Python 3.12, FastAPI, SQLAlchemy 2.0 (existing) | The hardened base. Python is also where the AI and ML work lives (embeddings, the estimation regressor, evaluation), so one language covers the API, workers and ML. | Go: faster per core, but the API is I/O bound and the rewrite cost would be large. Node: shares types with the SPA, but loses the hardened base and the ML ecosystem. |
| DB access | Synchronous SQLAlchemy in the threadpool (existing rule, CLAUDE.md §9). Revisit `asyncpg` if p95 is bound by threadpool size. | Consistent with every model, migration and test. Async gains nothing while queries are short. | Async SQLAlchemy / asyncpg |
| **Realtime gateway** | Node.js 24 with `ws` and **Hocuspocus** (Yjs) | Yjs's reference server is JavaScript. A small separate service keeps WebSockets off the API pods and scales on connection count. | Python `pycrdt-websocket` (less mature). Liveblocks (hosted; data residency and cost). |
| **Database** | **PostgreSQL 16+** with **pgvector**, `pg_trgm` and `citext`. Operated by CloudNativePG on Kubernetes, or managed (Aurora PostgreSQL, Cloud SQL, AlloyDB). | See §5.3. | MySQL 8 (the base's current database) |
| Connection pooling | PgBouncer in transaction mode | Many API and worker pods against a bounded number of Postgres connections. `SET LOCAL` keeps RLS safe in this mode. | Pgpool-II |
| **Cache, pub/sub, locks** | **Redis 7 / Valkey**: 3 nodes with Sentinel, or managed with replicas | Board and view cache with generation keys, presence, distributed rate limits, gateway fan-out. Always derived data. | Memcached (no pub/sub or streams) |
| **Queues and events** | Phase 1: Postgres (`ai_jobs` with `SKIP LOCKED`, plus an outbox). Phase 2: **Redis Streams** consumer groups. Phase 3: **Kafka / Redpanda** via Debezium CDC for the event bus. | Grow the infrastructure only when a measurement demands it. Every stage keeps at-least-once delivery and idempotent handlers. | Celery with RabbitMQ (another stateful system with its own failure modes); SQS (cloud lock-in, fine on AWS) |
| **Search** | Postgres full-text search and trigram in Phase 1. **OpenSearch 2.x** from Phase 2 for PQL `text ~`, cross-entity search and facets. | OpenSearch is Apache-2.0 licensed and runs k-NN too, if the vector workload ever outgrows pgvector. | Elasticsearch (licence terms need legal review for SaaS); Meilisearch/Typesense (simpler, but weaker for the complex boolean queries PQL compiles to) |
| **LLM** | **Claude** through the official **Anthropic Python SDK**. Default `claude-opus-5`, configurable per feature. Server-side fallbacks, structured outputs, prompt caching and the Batch API. | Strong at reasoning over long engineering threads. Structured outputs remove parsing. The SDK exposes new API features immediately. | LangChain on top: an abstraction the workload does not need (03 §3.2) |
| Embeddings | **Voyage AI** (e.g. `voyage-3.5`, 1024 dimensions), stored in pgvector with an HNSW index | Anthropic's recommended embeddings partner. Retrieval quality is strong on technical text. | OpenAI or Cohere embeddings; self-hosted `bge` models (operational cost) |
| **Orchestration** | Kubernetes (EKS/GKE) with Helm and **Argo CD** (GitOps), **KEDA** for queue-driven autoscaling, and HPA | Stateless services, rolling deploys, pod anti-affinity across AZs, scaling on queue depth for workers. | ECS/Fargate (simpler, but less portable) |
| Infrastructure as code | Terraform | Cells (01 §1.5) are stamped from one module. | Pulumi |
| Edge | Cloudflare or CloudFront, with WAF and a managed L7 load balancer | DDoS and bot protection, and static assets close to users | — |
| Observability | OpenTelemetry, Prometheus/Grafana, Tempo, Loki, and Sentry for the front end and back end | Open standards. The existing `X-Request-ID` becomes the trace ID. | Datadog (fast to adopt, expensive at scale) |
| Secrets | External Secrets with a cloud KMS or Vault. Envelope encryption for stored integration tokens. | Keeps keys out of images (already enforced by the base's `.dockerignore` check) and out of the database. | — |
| CI/CD | GitHub Actions (existing workflows), extended with the SPA workflow, image signing (cosign) and SBOMs | The base's `make verify` gates already run there. | — |

## 5.3 Why move from MySQL to PostgreSQL

The base ships on MySQL 8, and Phase 1's schema stays portable (it passes the same integration
tests on SQLite and MySQL 8). The product needs capabilities that only PostgreSQL provides
together:

| Need | PostgreSQL | MySQL 8 |
|---|---|---|
| Tenant isolation enforced by the database | **Row-level security** policies (tested, 02 §2.8) | None; views and definer routines only |
| Vector search for RAG | **pgvector** (HNSW, filtered iterative scans) next to the data, inside the same transactions and RLS | No vector search in community MySQL |
| Custom fields | **JSONB** with GIN (`jsonb_path_ops`) and expression indexes | JSON with generated-column indexes, one per path |
| Soft delete and hot subsets | **Partial indexes** (`WHERE deleted_at IS NULL`) | Not supported |
| Board rank ordering | `COLLATE "C"` per column | Binary collations work, but a case-insensitive default is a trap |
| Event log retention | Declarative partitioning with detach and archive | Partitioning, but foreign keys are not allowed on partitioned tables |
| Migrations | **Transactional DDL**: a failed revision rolls back cleanly | DDL commits implicitly, so a half-applied revision is possible |
| Change data capture | Logical replication (Debezium) | Binlog CDC (also good) |

The cost is about one engineer-week, and it is Phase 1 step 1 (06). It includes:

- Swapping the driver (`psycopg[binary]`).
- Fixing the one non-portable default in the baseline revision (`Boolean` with
  `server_default=text("0")`).
- Moving compose to `pgvector/pgvector:pg16`, and switching CI's MySQL service and the native
  bootstrap to PostgreSQL.
- Converting `CHAR(36)` keys to `uuid`.
- Updating the notes in `tests/conftest.py` that are specific to MySQL's isolation levels.

## 5.4 What is deliberately not in the stack yet

| Not yet | Add it when |
|---|---|
| Redis | A measured board p95 above about 120 ms, or the realtime gateway lands, whichever comes first |
| OpenSearch | PQL text search or cross-entity search is scheduled (Phase 2) |
| Kafka | Outbox throughput beyond what Redis Streams handles comfortably (roughly 10k events/s sustained), or several independent consumers need replay |
| A dedicated vector database | More than about 50–100M vectors in one cell, or filtered recall that pgvector's iterative scan cannot hold |
| Microservices beyond API, gateway and workers | A team boundary, not a technical one, asks for it |

Each addition is a new stateful system, and every stateful system needs its own backups,
failover and on-call. Adding one before it is needed creates operating cost without a
matching benefit.
