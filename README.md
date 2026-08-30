# ai-server

> kiko.ai AI search server. FastAPI pipeline over FashionSigLIP embeddings (Modal) + Supabase pgvector/pgroonga hybrid search with RRF.

`endurance-ai/kiko.ai-app` (Next.js) calls `/recommend` after Instagram scrape + Vision analysis. The kiko app and web clients consume the same pipeline through a LangGraph StateGraph over the `/chat` SSE endpoint.

```
[Vercel / Next.js]              [EC2 / Docker Compose]            [Modal Serverless]
──────────────────              ──────────────────────            ──────────────────
Apify + R2 + Vision       →     ai-server (this repo)       ↔    /embed (FashionSigLIP)
session / auth / UI             LangGraph + LiteLLM
                                Langfuse self-host
                                       ↑
                          app / web clients — /chat SSE (LangGraph)
```

## Quickstart

```bash
uv sync
cp .env.example .env
# fill in Supabase, Modal, LiteLLM keys
uv run uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health
```

## Validate

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

## Deploy

GitHub Actions on `dev` merge → ECR push → SSH deploy to EC2 t4g.medium (docker-compose). See `docs/infra/cicd.md`.

## Layout

```
app/
├── main.py              # FastAPI entrypoint + lifespan
├── api/                 # routers (chat SSE, auth, recommend, health)
├── channels/            # channel adapters, recommendation port, link_resolver, vision, session
├── graphs/              # LangGraph StateGraph (10 nodes) + routing
├── pipeline/            # embed → enhance_query → search → diversify
├── providers/           # SupabaseProvider, EmbedProvider, LLMProvider
├── observability/       # Langfuse @observe wrapper
├── models/              # Pydantic v2 request/response
└── core/                # config (env)
```

## Responsibility split

| Layer | Role |
|-------|------|
| Vercel / `kiko.ai-app` | Apify, R2, Vision (GPT-4o-mini), session, UI, v4 fallback |
| **ai-server (this repo)** | search orchestration, enhance_query, Langfuse trace, `/chat` SSE + channel adapters |
| Modal | FashionSigLIP embeddings (single + batch) |
| Supabase | pgvector + pgroonga, `search_products_v5` RPC |

## Core stack

| Area | Choice |
|------|--------|
| Framework | FastAPI + uvicorn |
| Agent orchestration | LangGraph >=1.1.10 |
| LLM | LiteLLM proxy (httpx) + langchain-openai |
| Embeddings | Modal HTTP endpoint (FashionSigLIP) |
| Vector DB | Supabase pgvector + pgroonga (no Qdrant) |
| Observability | Langfuse self-host (LiteLLM callback + `@observe`) |
| Schema | Pydantic v2 |
| Package / lint / test | uv / ruff / pytest |
| Container | Docker (multi-stage uv) |

## Search responsibility

```
[Postgres RPC] dense (HNSW) + sparse (pgroonga) + RRF → top-50
       ↓
[Python] diversity cap (brand/platform) + tolerance + final sort → top-15
```

## Auth

AI server is stateless — no auth on this side. `kiko.ai-app` owns session + Supabase Auth and passes the resolved context via request body. `/recommend` is gated by `X-Internal-Token`; app/web endpoints (`/chat`, `/me`, ...) by the app JWT.

## Related projects

| Project | Repo | Role |
|---------|------|------|
| kiko.ai-app | endurance-ai/kiko.ai-app | Next.js monolith (caller + v4 fallback) |
| crawler | endurance-ai/crawler | Cafe24 + Shopify SKU harvester |
| aws-infra | private | EC2 docker-compose + Langfuse + Modal infra |

## Notes

- Internal — kiko.ai team only.
- Langfuse SDK pinned to `>=2.50,<3.0` (server is v2 image; v3 SDK changed the ingestion endpoint).
- LangGraph 1.x requires `langchain-core>=1.3` — pinned together to keep compatibility with `langfuse v2 + langchain` callback wrapping.
