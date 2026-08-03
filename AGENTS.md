# Codex Guide: ai-server

This repository is the product core: FastAPI service, Telegram webhook, LangGraph/ReAct conversation agent, recommendation/search orchestration, and observability.

## Current Product Shape

- Telegram bot `@kiko_fashion_ai_bot` is the primary user-facing product.
- The server talks to FashionSigLIP embeddings, LiteLLM/Bedrock, Langfuse, Redis chat-state, and dev-app Postgres/PostgREST.
- `kiko.ai-app` owns the main DB schema; this repo consumes the search RPC and AI schema.
- Prefer `CLAUDE.md` over the older `README.md` when they conflict; the README contains some stale v5/v2 notes.

## Stack

- Python 3.13+, FastAPI, uvicorn
- uv for dependency management
- LangGraph, LangChain/OpenAI-compatible client via LiteLLM
- Pydantic v2, httpx, psycopg, redis
- Ruff for lint/format, pytest for tests

## Commands

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Before commit/PR-quality handoff, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Key Directories

- `app/main.py`: FastAPI app, lifespan, router registration
- `app/api/`: recommend, health, Telegram webhook, debug endpoints
- `app/agents/`: ReAct loop, tool registry, memory/reflexion helpers, tools
- `app/channels/`: messenger adapters, Telegram implementation, persona, language, vision, link resolving
- `app/graphs/`: LangGraph StateGraph, nodes, routing, state models
- `app/services/`: business services for embed/search/diversify/database
- `app/infrastructure/`: repositories, Redis chat state, memory persistence, RPC contract
- `app/providers/`: DB/PostgREST, Modal embedding, LiteLLM, embedding cache
- `app/observability/`: Langfuse and conversation log
- `tests/`: characterization, graph, agent, API, auth, observability, and integration-style tests

## Development Rules

- Preserve the single permanent LangGraph topology unless the task explicitly changes architecture.
- Treat Telegram transport as a black box; channel-specific behavior belongs under `app/channels/`.
- Keep channel-to-pipeline coupling through protocol/port boundaries such as `app/channels/recommendation.py`.
- Keep ReAct tool definitions and validation centralized in `app/agents/tool_registry.py`.
- Keep user input fenced or clearly separated from system-derived context in prompts.
- Maintain sticky language behavior through `app/channels/lang.py`.
- Use fail-open behavior intentionally for observability, Redis chat-state, and logging helpers where the existing code does.
- Do not print secrets from `.env` or `.env.local`.

## Harness and Quality Policy

Mirror the MoAI harness intent from `.moai/config/sections/`:

- Development mode is DDD: analyze existing behavior, preserve it, then improve.
- Add or update characterization tests before risky behavior changes when existing behavior is not already covered.
- Keep transformations small; avoid broad rewrites unless explicitly requested or necessary.
- Use minimal validation for docs/config/simple bugfixes, standard validation for features/refactors or multi-file changes, and thorough validation for auth, security, migrations, public APIs, Telegram webhook behavior, agent graph topology, or critical fixes.
- Escalate validation depth after any quality gate failure or critical review finding.
- Maintain zero new lint/type/test regressions. If a pre-existing failure blocks verification, report it clearly.
- Prefer behavior/spec tests with meaningful assertions over implementation-coupled tests.

## Docs to Read/Update

- Architecture: `docs/ARCHITECTURE.md`
- Patterns: `docs/PATTERNS.md`
- Pipeline: `docs/features/pipeline.md`
- Search: `docs/features/search-engine.md`
- Search RPC contract: `docs/infra/search-rpc-contract.md`
- Env: `docs/infra/env.md`
- Deployment/CICD: `docs/infra/deployment.md`, `docs/infra/cicd.md`

Update docs when behavior, topology, env vars, search contract, or operational steps change.

## Validation

- Pure helper/model change: targeted `uv run pytest path/to/test.py`.
- Agent/graph/search changes: targeted tests plus the relevant broader suite.
- Before finalizing substantial changes: `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
- If tests need external services or credentials, use existing stubs/fakes where possible and report any skipped checks.

## Git Policy

- Manual workflow: do not create branches, push, or open PRs unless the user asks.
- If asked to commit, use conventional commit style and keep the header near 72 chars.
- Do not use broad staging; stage only the files intended for the change.
