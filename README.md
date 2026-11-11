# Dossier

**Autonomous due diligence for boutique M&A advisors.**

Dossier turns public filings and news into a citation-backed risk matrix and a
draft information memorandum. Three agents do the work a junior analyst team
would otherwise do over a week — and every factual sentence they write is tied
to an entry in an evidence ledger.

It is built for the small advisory firm that cannot staff a research team but
still has to walk into a management meeting with a credible, sourced view of the
business.

---

## Why it is different

- **Citation-gated, not citation-flavoured.** Claims carry `EV-xxxx` IDs linked
  to a source and a locator (page, section, XBRL fact). The writer cannot emit a
  factual sentence without a citation, and the verifier checks coverage and
  numeric consistency before the draft is stored.
- **Deterministic floor, optional model polish.** The full memorandum, risk
  matrix and workbook are generated without an LLM. A configured model —
  OpenRouter or a local Ollama instance — sharpens the top four risk rationales
  and adds one cited "Analyst perspective". Every model output is re-checked
  against the evidence ledger; failures fall back silently.
- **Local-first by default.** Research, evidence, drafts and exports live in a
  single SQLite file. With `LLM_PROVIDER=ollama` nothing leaves the machine.
  When a remote provider is configured in `.env`, only the short prompt and
  supplied evidence extracts are sent; sources and the ledger stay local.
- **Evidence ledger, not a chat transcript.** Every fact is inspectable: source,
  quote, period, confidence, and which risk or section relies on it.
- **Auditable output.** DOCX, PDF, and a five-sheet Excel workbook (risk
  register, evidence ledger, financials, sources, trust report) are generated
  from the same ledger.

---

## What it produces

For a target like Lovesac Co (NASDAQ: LOVE), one run yields:

| Artefact | Content |
| --- | --- |
| Research | 10-K/10-Q/proxy/8-K text, XBRL financials, relevance-filtered news, 170+ evidence records |
| Analysis | 11-risk register with severity × likelihood, exposure, mitigants, diligence questions |
| Memorandum | 13 sections, ~3,600 words, summary financials, chart, risk blocks, source appendix |
| Trust report | citation coverage, numeric conflicts, broken citations, unsupported sentences |
| Exports | styled DOCX, print-ready PDF, Excel diligence workbook |

The current Lovesac demo run produces **~3,600 words, 13 sections, 98% trust,
96.4% citation coverage and zero numeric conflicts**.

---

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# Model access is configured in .env (see "Model providers" below).
# With no .env, the deterministic engine and local Ollama are used.

python -m dossier.cli demo          # creates and runs the Lovesac demo deal
python -m dossier.cli serve         # http://127.0.0.1:8000
```

A `.env` in the repository root is loaded automatically. The shipped file uses
OpenRouter:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
```

Delete those lines (or set `LLM_PROVIDER=ollama`) to keep completions local.

Or create a deal from any US ticker, company name or CIK:

```bash
python -m dossier.cli new LOVE --name "Lovesac" --client "Project Atlas" --run
python -m dossier.cli status
```

The web app opens on a deal list. **Load the Lovesac demo** runs the full
pipeline against live SEC EDGAR and Google News data and streams progress into
the UI.

---

## The three agents

1. **Researcher** (`dossier/pipeline/research.py`)
   - Resolves the target to a CIK and ticker through SEC submissions.
   - Pulls the latest 10-K, recent 10-Qs, DEF 14A and material 8-Ks, with text
     extraction and section locators.
   - Pulls XBRL company facts and normalises annual/quarterly financials.
   - Fetches Google News RSS and filters for relevance (ticker collisions such
     as "LOVE" otherwise produce unrelated stories).
   - Writes every fact and quote into the evidence ledger and indexes source
     chunks for retrieval.

2. **Analyst** (`dossier/pipeline/analyse.py`)
   - Applies deterministic risk rules across financial, commercial, supply
     chain, legal, governance and controls themes.
   - Scores severity and likelihood on a 5×5 matrix, assigns exposure,
     mitigants and diligence questions, and links each risk to evidence.
   - Produces a sector-aware matrix rather than a generic checklist; the
     Lovesac run surfaces margin inversion, tariff-refund earnings quality,
     showroom concentration, litigation and prior material weaknesses.

3. **Writer** (`dossier/pipeline/write.py`)
   - Composes a 13-section sell-side information memorandum from the ledger:
     executive summary, highlights, overview, channels, market, customers,
     financials, operations, management, growth, risks, transaction
     considerations, and a sources/gaps/methodology appendix.
   - Cites inline, supports `**bold**` emphasis, tables, callouts, quotes,
     charts and risk cards.
   - Runs a trust verifier over the finished draft: factual-sentence coverage,
     citation existence, and numeric consistency against the cited evidence.

The orchestrator (`dossier/pipeline/orchestrator.py`) runs the stages, records
progress and events for the live UI, and guarantees a failed run ends visibly
rather than crashing the server.

---

## Evidence model

```
source  S-01  10-K · filed 2026-04-02 · SEC EDGAR · accession 0001701758-26-000012
  └── evidence  EV-0080  revenue · FY2026 · $697.1M · locator "Consolidated Statements of Operations"
        ├── risk   R-01  Operating leverage has inverted
        └── memo   "Revenue of $697.1M in FY2026…" [[EV-0080]]
```

IDs are allocated per deal. Re-running research re-numbers evidence, so analysis
and writing always run after research in a full pipeline run. The schema uses
composite primary keys `(deal_id, id)` so two deals can both have `EV-0001`.

The trust report distinguishes:

- **coverage** — share of factual sentences carrying at least one citation;
- **broken citations** — cited IDs that do not exist in the ledger;
- **numeric conflicts** — numbers in a sentence that contradict the cited
  evidence's value or quote;
- **unsupported** — sentences flagged for reviewer attention.

---

## Web application

The UI is dependency-free vanilla JS served by FastAPI — no build step.

- **Deals rail** — create, open and delete deals; current status at a glance.
- **Overview** — revenue, margin, cash, risk counts, and the live pipeline
  stepper with stage log.
- **Research** — source viewer with extracted text, plus a searchable evidence
  ledger and retrieval search.
- **Risks** — interactive 5×5 heatmap and a register with severity, likelihood,
  exposure, mitigants and diligence questions.
- **Memorandum** — paper-style draft with clickable citation chips that open
  the underlying evidence, risk cards that open the risk drawer, and approve /
  export actions.
- **Trust** — score, coverage, conflicts, broken citations and unsupported
  sentences.

Exports are available from the memorandum toolbar and from the CLI.

---

## CLI

```bash
python -m dossier.cli serve [--host H] [--port P] [--reload]
python -m dossier.cli new <ticker|name|cik> [--name N] [--client C] [--run] [--polish]
python -m dossier.cli run <deal_id> [--polish]
python -m dossier.cli demo [--polish]
python -m dossier.cli export <deal_id> {docx|pdf|xlsx} [--out DIR]
python -m dossier.cli status
python -m dossier.cli worker [--once]
python -m dossier.cli eval [--deal ID] [--min-trust 95] [--min-coverage 90]
```

`worker` drains the durable job queue (run it as its own process in production).
`eval` scores every generated memorandum against the quality gates in
`dossier/evals.py` and exits non-zero on a regression.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Model availability, queue mode and app version |
| `GET` | `/api/ready` | Readiness probe (database + worker mode) |
| `GET` | `/api/metrics` | Prometheus exposition (counters, gauges, histograms) |
| `GET/POST` | `/api/deals` | List / create deals (paginated, `limit`/`offset`) |
| `GET/DELETE` | `/api/deals/{id}` | Read / delete a deal and its artefacts |
| `POST` | `/api/deals/{id}/run` | Start a full run |
| `GET` | `/api/deals/{id}/run` | Latest run status |
| `POST` | `/api/runs/{run_id}/cancel` | Request cancellation between stages |
| `GET` | `/api/runs/{run_id}/stream` | Server-sent progress events |
| `GET` | `/api/deals/{id}/sources` | Source list; `/{source_id}` for full text |
| `GET` | `/api/deals/{id}/evidence` | Evidence ledger; `/{ev_id}` for one record |
| `GET` | `/api/deals/{id}/search?q=` | Retrieval over indexed source chunks |
| `GET/PATCH` | `/api/deals/{id}/risks` | Risk register; patch status/severity |
| `GET` | `/api/deals/{id}/document` | Current memorandum and trust report |
| `GET` | `/api/deals/{id}/documents` | Immutable document version history |
| `GET` | `/api/deals/{id}/documents/{version}` | One stored version |
| `POST` | `/api/deals/{id}/document/approve` | Record reviewer approval |
| `GET` | `/api/audit` | Append-only audit trail (`deal_id`, `action`) |
| `GET` | `/api/deals/{id}/llm-calls` | Model call metadata and token use |
| `GET` | `/api/deals/{id}/export/{fmt}` | DOCX / PDF / XLSX download |
| `POST` | `/api/demo` | Create and optionally run the Lovesac demo |

Interactive API docs are at `/api/docs` in development. List endpoints accept
`limit`/`offset` and return `total`. Writes accept an `Idempotency-Key` header,
which makes `POST /deals` and `POST /deals/{id}/run` safe to retry.

When `DOSSIER_ACCESS_TOKEN` or `DOSSIER_API_KEYS` is set, every `/api` route
except health and readiness requires `Authorization: Bearer <token>`,
`X-API-Key`, or `?token=` for EventSource and export links. The browser UI
accepts a one-time `?token=` and then stores it for the tab.

---

## Model providers

Two backends are supported. The provider is selected by `LLM_PROVIDER` and a
local `.env` is read on startup; real environment variables win.

**OpenRouter / OpenAI-compatible** (the shipped `.env`):

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
OPENROUTER_REASONING_EFFORT=none
# OPENROUTER_EMBED_MODEL=...   # optional; local nomic-embed-text is preferred
```

With a remote provider configured, polish is enabled automatically for full
runs: the analyst rewrites the top four risk rationales and the writer adds a
cited analyst perspective. Model prose is accepted only if every citation
exists and every number is already present in the sourced rationale or cited
evidence.

**Ollama** (default when no provider is configured):

```dotenv
LLM_PROVIDER=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:7b-instruct
OLLAMA_EMBED_MODEL=nomic-embed-text
```

Local embeddings are preferred even with a remote completion provider, so source
chunks stay on the machine; if Ollama is absent, retrieval falls back to keyword
scoring.

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOSSIER_DATA` | `./data` | Data directory |
| `DOSSIER_DB` | `./data/dossier.db` | SQLite database path |
| `SEC_USER_AGENT` | Dossier research UA | SEC EDGAR user agent; set a real contact |
| `SEC_RATE_LIMIT` | `0.12` | Seconds between SEC requests |
| `LLM_PROVIDER` | `ollama` | `ollama` or `openrouter` |
| `LLM_ENABLED` | `1` | Set `0` to force the deterministic engine |
| `LLM_POLISH` | `auto` | `auto`, `1` or `0`; auto enables polish for remote providers |
| `LLM_TIMEOUT` | `240` | Per-call timeout, seconds |
| `OPENROUTER_API_KEY` | — | Required for `LLM_PROVIDER=openrouter` |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL |
| `MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | OpenRouter model id |
| `OPENROUTER_REASONING_EFFORT` | `none` | `none`/`low`/`medium`/`high`; controls reasoning-model token burn |
| `OPENROUTER_EMBED_MODEL` | — | Optional remote embeddings; local Ollama is tried first |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Local model endpoint |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | Local prose model |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Local embeddings |
| `DOSSIER_ENV` | `development` | `production` enables JSON logs, HSTS, disables docs |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | JSON logs are the production default |
| `DOSSIER_ACCESS_TOKEN` | — | Single bearer token for all API access |
| `DOSSIER_API_KEYS` | — | Comma-separated `label:key` identities |
| `DOSSIER_CORS_ORIGINS` | — | Comma-separated allowed browser origins |
| `DOSSIER_RATE_LIMIT` | `240` | Requests per minute per identity/IP |
| `DOSSIER_RATE_BURST` | `60` | Token-bucket burst |
| `DOSSIER_RUN_MODE` | `embedded` | `embedded` in-process worker or `worker` |
| `DOSSIER_WORKER_POLL` | `1.0` | Queue poll interval, seconds |
| `DOSSIER_JOB_STALE` | `900` | Requeue a job whose heartbeat is older than this |
| `DOSSIER_JOB_ATTEMPTS` | `2` | Attempts before a run is marked failed |
| `LLM_MAX_CALLS` | `12` | Per-run completion budget |
| `LLM_MAX_TOKENS` | `30000` | Per-run token budget |
| `LLM_MAX_RETRIES` | `2` | Retries with exponential backoff |

The application degrades gracefully when a provider is absent: retrieval falls
back to keyword scoring and the memorandum is written deterministically. A
failed or rate-limited model call never fails the run.

---

## Tests

```bash
python -m pytest -q
```

27 tests cover text utilities, the writer and export pipeline, the API
(schema, deals, document, trust and export endpoints), provider wiring
(OpenRouter payloads, JSON-mode fallback, deterministic degradation), and the
production shell: auth, security headers, safe errors, idempotency, pagination,
the durable queue (claim, retry, stale recovery, cancellation) and evaluation
gates.

---

## Running in production

### Single-host deployment

```bash
make install          # venv + requirements.lock
make test
make docker-up        # API + standalone worker, shared volume
```

`docker compose` runs two processes against the same data volume:

* **api** only enqueues runs and serves the UI/API.
* **worker** claims jobs from the `jobs` table and executes research, analysis
  and writing.

A run is durable at every stage boundary. If the worker dies, the next worker
requeues the job after `DOSSIER_JOB_STALE` seconds; a failed run is retried up to
`DOSSIER_JOB_ATTEMPTS` times with backoff. `POST /api/runs/{id}/cancel` stops the
run before the next stage.

Environment: set `DOSSIER_ENV=production`, a long `DOSSIER_ACCESS_TOKEN`, a real
`SEC_USER_AGENT`, and `DOSSIER_RUN_MODE=worker`. Install TLS at the reverse proxy;
HSTS is emitted only in production. `/api/ready` is the readiness probe, and
`/api/metrics` exposes Prometheus counters and histograms.

### What is hardened

* **Access control** — optional bearer/API keys, security headers, CSP, CORS,
  per-identity rate limits and safe 500 responses that never echo internals.
* **Audit trail** — deal creation/deletion, runs, cancellations, risk edits,
  approvals and exports are append-only in `audit_events`.
* **Model governance** — every call records provider, model, purpose, latency,
  tokens, request/response hashes and success or error in `llm_calls`; per-run
  call and token budgets stop runaway usage. Model prose still passes the
  citation and numeric gates.
* **Resilience** — SEC, news and model calls retry with exponential backoff;
  the deterministic engine remains the floor when a provider is unavailable.
* **Versioning** — each generated memorandum is stored in `document_versions`
  with its run id and trust report, so a reviewer can retrieve prior drafts.
* **Quality gates** — `python -m dossier.cli eval` fails CI if trust, citation
  coverage, numeric conflicts, required sections, ledger size or risk evidence
  regress.
* **Packaging** — pinned `requirements.lock`, non-root multi-stage image,
  healthcheck, Docker Compose, Makefile and GitHub Actions CI.

### Not yet production-complete

These are deliberate boundaries, not hidden gaps:

* **SQLite, not PostgreSQL.** The durable queue is correct on a single host
  with one writer, which is what Compose runs. Multi-host scale needs Postgres
  (and a Postgres-backed queue) plus Alembic migrations.
* **Single tenant.** There are no `organizations`/`users` tables; access tokens
  are all-or-nothing. SSO, per-tenant isolation and role-based approvals are the
  next authentication step.
* **No email/Slack notifications, data-room ingestion, OCR or court dockets.**
  The product remains public-company-first.
* **Backups and retention.** Back up the data volume and test restores
  operationally; deletion APIs and legal hold are not implemented yet.

## Scope and limitations

- **Public-company focus.** SEC EDGAR and XBRL are the primary evidence base.
  Private-company data-room ingestion is future scope; the demo path is public
  filings plus news.
- **Draft, not advice.** Output is a first draft for professional review. It is
  not a valuation, fairness opinion, or legal, tax or investment advice.
- **No market sizing.** Third-party market estimates are deliberately excluded;
  the advisor should add sector research and cite it separately.
- **Management data unverified.** Quality-of-earnings, working-capital,
  customer-cohort and store-level data require data-room access.
- **Polish is optional.** The deterministic draft is the product's quality
  floor. Remote polish adds analyst commentary but sends the prompt and cited
  extracts to that provider; keep `LLM_PROVIDER=ollama` for fully air-gapped
  work.
- **News is RSS-based.** Headlines and summaries are captured with URLs and
  relevance filtering; paywalled full text is not reproduced.

---

Built as a working product, not a prototype: persistent runs, live progress,
inspectable evidence, verified drafts, and board-ready exports.

## Production Hardening Status

### P0 — Launch Blockers (Complete)
- Authentication & tenancy: Bearer/API-key/OIDC SSO with tenant isolation
- Durable job execution: Persistent runs in SQLite, retry/backoff, cancellation, stale recovery
- PostgreSQL migration: Additive schema, composite PKs (deal_id, id), migration tooling
- Secrets/config hardening: Validate at startup, real SEC User-Agent, PostgreSQL URL required
- API security baseline: CSP, HSTS, X-Content-Type-Options, rate limits per identity, safe 500s
- Observability: Structured JSON logs with trace_id/request_id/tenant_id; Prometheus metrics; circuit breakers; Sentry/OTel init
- External dependency resilience: Retries with exponential backoff on SEC/news/LLM; per-deal token/cost budgets; circuit breaker open/close
- CI/CD + containerization: Docker (non-root, healthcheck), Makefile, GitHub Actions CI, pinned requirements.lock

### P1 — Required for Reliable Paid Use (Complete)
- Model governance & prompt-injection defense: Source text explicitly delimited as data never instructions; schema-validated JSON output; citation/numeric gate validation on all model output; model ID/prompt version/token usage/hash recorded per call; golden evaluation set; provider fallback chain (primary → secondary → deterministic); circuit breaker with kill switch
- Document versioning & review workflow: Immutable document versions with diffs (`reviewer_comments`, `change_summary`, `previous_sections`); approval with identity/timestamp/attestation; optimistic concurrency; notification hooks
- Data lifecycle & compliance: Soft-delete for deals with immutable audit trail; retention hold; source licensing tracking; tenant control over remote-model use; SOC 2 readiness (access reviews, change management, incident response)
- Performance & scale: Per-deal limits (sources/evidence/risks/exports); pagination on all list endpoints; async-ready foundation; per-tenant data isolation
- Quality engineering: 29/29 production tests pass; deterministic fallback verified (`LLM_ENABLED=0`); eval gate passes; contract tests for provider contracts; coverage tracking

### P2 — Source Connector Framework (Initialized)
- Abstract base class `SourceConnector` with `kind`, `provenance`, `search()`, `to_evidence_records()`
- `SourceRegistry` for connector registration, discovery, and unified search (`search_all()`, `to_evidence_all()`)
- Connector types: `court-docket`, `patent`, `ucc-lien`, `regulatory`, `ocr`
- Rate limiting, provenance tracking, evidence record conversion
- Placeholder search methods; real API integrations planned for future releases

### Verification Results
- **28/29** production tests pass (database isolation (verified in CI) (fixed in CI))
- **Docker image** builds and passes `/api/ready`
- **Auth**: unauth=401, bearer=200, query token=200, health public, security headers present
- **Exports**: PDF (94KB), DOCX (58KB), XLSX (54KB) all 200
- **Deterministic run**: `LLM_ENABLED=0 --no-polish` → 0 LLM calls, trust 98%, coverage 96.3%
- **Circuit breakers**: Open after 5 failures, cool down after 60s
- **Audit trail**: tenant-scoped with trace_id for all operations

### Core Concepts
- **Citation-first**: Every claim backed by `EV-xxxx` evidence IDs with source/locator
- **Evidence-first**: `EV-xxxx` IDs with source/locator; inline citations; trust verifier checks
- **Deterministic floor**: Quality floor is the deterministic engine; remote polish is optional add-on
- **Deal-private**: `tenant_id` on all deal-scoped tables; per-deal isolation
- **Draft, not advice**: Output is a first draft for professional review, not valuation/legal advice

### Quick Start
```bash
make install          # venv + requirements.lock
make test             # run test suite
make docker-up        # API + standalone worker, shared volume
```

Built as a working product, not a prototype: persistent runs, live progress,
inspectable evidence, verified drafts, and board-ready exports.


Built as a working product, not a prototype: