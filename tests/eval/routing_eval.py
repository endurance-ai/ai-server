"""라우팅 eval — 44케이스(`routing_dataset.json`)로 모델 arm 을 비교한다.

2026-09-07 모델 티어링 결정(fea09f4, "Haiku 81% vs Sonnet 98.5%")은 세션 스크래치
스크립트로 쟀고 저장소에 남지 않아 재현이 안 됐다. 그 스크립트의 케이스·채점을
그대로 옮겨 두 모드로 다시 잴 수 있게 한다.

--mode isolated (9/7 슬라이드 수치의 방식)
    정적 시스템 프롬프트 + 유저 발화 한 번만 LLM 에 보내고 **첫 도구**가
    expect_tools 에 있고 forbid_tools 에 없으면 성공. 결정론 라우터(맨-브랜드,
    brand-similar)·동적 컨텍스트·실제 검색은 거치지 않는다. prior 는 실제 직전
    검색이 아니라 "[직전 대화 맥락: …]" 한 줄로만 준다.

--mode e2e (실제 턴)
    run_react_loop 를 끝까지 돌린다(prior 는 setup 검색을 실제로 한 번 실행).
    세 축 모두 통과해야 성공:
      route   첫 도구 ∈ expect_tools, ∉ forbid_tools
      cards   cards_expected 면 카드가 실제 전달됐는가
      honest  응답이 결과를 약속하면("골라봤어/here are…") 카드가 있는가
    결정론 라우터가 LLM 대신 처리한 턴은 `deterministic` 으로 따로 센다 —
    그 턴의 성공은 모델 비교가 아니다.

배포된 프롬프트·도구 스키마·LiteLLM 을 써야 하므로 ai-server 컨테이너 안에서 돌린다:
    docker cp tests/eval/routing_eval.py ai-server:/tmp/
    docker cp tests/eval/routing_dataset.json ai-server:/tmp/
    docker exec -w /app ai-server env PYTHONPATH=/app python /tmp/routing_eval.py \\
        --mode isolated --runs 3 --output /tmp/routing_isolated.json
    docker exec -w /app ai-server env PYTHONPATH=/app python /tmp/routing_eval.py \\
        --mode e2e --output /tmp/routing_e2e.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DATASET = Path(__file__).with_name("routing_dataset.json")

# arm 이름 → (base 모델, router 모델). router 빈값 = 전 구간 base.
ARMS: dict[str, tuple[str, str]] = {
    "A_haiku": ("claude-haiku-4-5", ""),
    "B_sonnet_router": ("claude-haiku-4-5", "claude-sonnet-4-5"),
    "C_all_sonnet": ("claude-sonnet-4-5", ""),
}
# isolated 모드는 첫 호출 한 번만 보므로 B 와 C 가 같다 → 모델 둘만.
ISOLATED_MODELS = ["claude-haiku-4-5", "claude-sonnet-4-5"]

_PROMISE_RE = re.compile(
    r"찾아봤|골라봤|골라왔|나왔어|몇 개|추천.*(?:이야|해|줄게)|here are|picks|pulled|got (?:a |some|you)", re.I
)


def load_cases(limit: int = 0) -> list[dict[str, Any]]:
    cases = json.loads(_DATASET.read_text())["cases"]
    return cases[:limit] if limit > 0 else cases


def route_ok(case: dict[str, Any], tool: str) -> bool:
    return tool in case["expect_tools"] and tool not in case["forbid_tools"]


def _pct(oks: list[bool]) -> float:
    return round(100 * sum(oks) / len(oks), 1) if oks else 0.0


def _by_cat(rows: list[tuple[str, bool]]) -> dict[str, str]:
    cats: dict[str, list[bool]] = {}
    for cat, ok in rows:
        cats.setdefault(cat, []).append(ok)
    return {k: f"{sum(v)}/{len(v)}" for k, v in sorted(cats.items())}


# ── isolated ────────────────────────────────────────────────────────────────


def _isolated_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    from app.agents.react_loop import _STATIC_SYSTEM_PROMPT_KO

    user = case["input"]
    if case.get("prior"):
        user = f"[직전 대화 맥락: {case['prior']}]\n\n{user}"
    return [{"role": "system", "content": _STATIC_SYSTEM_PROMPT_KO}, {"role": "user", "content": user}]


def _first_tool(ai: Any) -> str:
    tcs = getattr(ai, "tool_calls", None) or []
    if not tcs:
        return "respond"
    tc = tcs[0]
    return (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) or "respond"


async def run_isolated(cases: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    from app.agents.llm_client import _build_tools_schema
    from app.core.config import settings
    from app.providers.litellm_chat import LiteLLMChatOpenAI

    out: dict[str, Any] = {}
    for model in ISOLATED_MODELS:
        client = LiteLLMChatOpenAI(
            model=model,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=settings.LITELLM_MASTER_KEY or "x",
            temperature=0.4,  # 프로덕션 동일
            timeout=60.0,
        ).bind_tools(_build_tools_schema(), tool_choice=None)
        per_case: dict[str, list[str]] = {c["id"]: [] for c in cases}
        run_accs: list[float] = []
        cat_rows: list[tuple[str, bool]] = []
        for _ in range(runs):
            oks = []
            for c in cases:
                try:
                    tool = _first_tool(await client.ainvoke(_isolated_messages(c)))
                except Exception as exc:  # noqa: BLE001
                    tool = f"ERROR:{type(exc).__name__}"
                ok = route_ok(c, tool)
                oks.append(ok)
                cat_rows.append((c["category"], ok))
                per_case[c["id"]].append(tool)
            run_accs.append(_pct(oks))
        out[model] = {
            "acc_mean": round(statistics.mean(run_accs), 1),
            "runs": run_accs,
            "by_cat": _by_cat(cat_rows),
            "fails": {
                cid: tools
                for cid, tools in per_case.items()
                if not all(route_ok(next(c for c in cases if c["id"] == cid), t) for t in tools)
            },
        }
        r = out[model]
        print(f"[isolated] {model}: {r['acc_mean']}% runs={r['runs']} by_cat={r['by_cat']}", flush=True)
    return out


# ── e2e ─────────────────────────────────────────────────────────────────────


class _MockAdapter:
    """react_loop 가 보내는 텍스트·카드를 캡처(실제 채널 전송 대체)."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards = 0

    async def send_text(self, chat_id, text):
        self.texts.append(text)

    async def send_card(self, chat_id, card):
        self.cards += 1
        return self.cards

    async def send_chat_action(self, chat_id, action="typing"):
        return True

    async def send_progress(self, chat_id, stage):
        return True

    async def send_text_with_keyboard(self, chat_id, text, buttons):
        self.texts.append(text)

    async def send_text_with_buttons(self, chat_id, text, buttons):
        self.texts.append(text)

    async def send_media_group(self, chat_id, media):
        return True


def _set_models(base: str, router: str) -> None:
    from app.core.config import settings

    for k, v in (("AGENT_LLM_MODEL", base), ("AGENT_ROUTER_LLM_MODEL", router)):
        try:
            setattr(settings, k, v)
        except Exception:  # noqa: BLE001
            object.__setattr__(settings, k, v)


def _state(msg: str, chat_id: int):
    from app.channels.schemas import ChannelMessage
    from app.graphs.state import WorkingState

    cm = ChannelMessage(chat_id=chat_id, from_user_id=chat_id, text=msg, received_at=datetime.now(UTC))
    # eval 유저엔 성별 프로필이 없다 → unisex 로 성별 카드(awaiting_gender)에서 멈추지 않게.
    return WorkingState(message=cm, chat_id=chat_id, from_user_id=chat_id, req_gender="unisex")


def _setup_msg(prior: str) -> str:
    return f"{prior.replace(' 검색 완료', '').split('(')[0].strip()} 찾아줘"


_DETERMINISTIC: list[str] = []


def _wrap_deterministic_routers() -> None:
    """결정론 라우터가 턴을 가져갔는지 기록한다(LLM 라우팅이 아니므로 따로 센다)."""
    import app.agents.react_loop as rl

    for name in ("_run_bare_brand_shortcircuit", "_run_brand_similar_shortcircuit"):
        orig = getattr(rl, name)

        async def wrapped(*a, _orig=orig, _name=name, **kw):
            _DETERMINISTIC.append(_name)
            return await _orig(*a, **kw)

        setattr(rl, name, wrapped)


async def _run_case(case: dict[str, Any], chat_id: int) -> dict[str, Any]:
    from app.agents.react_loop import run_react_loop
    from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
    from app.infrastructure.memory.session import get_store

    store = get_store()
    if case.get("prior"):
        try:
            await run_react_loop(_state(_setup_msg(case["prior"]), chat_id), store.get_or_create(chat_id))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"setup:{type(exc).__name__}:{exc}", "lat_ms": 0}
    _DETERMINISTIC.clear()
    adapter = _MockAdapter()
    tok = set_adapter(adapter)
    t0 = time.perf_counter()
    try:
        delta = await run_react_loop(_state(case["input"], chat_id), store.get_or_create(chat_id))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}", "lat_ms": int((time.perf_counter() - t0) * 1000)}
    finally:
        reset_adapter(tok)
    lat_ms = int((time.perf_counter() - t0) * 1000)

    hist = delta.get("tool_call_history") or []
    tools = [h.get("tool_name") for h in hist]
    first = tools[0] if tools else "respond"
    resp = (delta.get("response_text") or " ".join(adapter.texts) or "").strip()
    delivered = adapter.cards > 0
    r_ok = route_ok(case, first)
    c_ok = (not case["cards_expected"]) or delivered
    promised = bool(_PROMISE_RE.search(resp))
    h_ok = (not promised) or delivered
    queries = [
        json.dumps(h.get("args"), ensure_ascii=False)[:200]
        for h in hist
        if h.get("tool_name") in ("search_products", "refine_search")
    ]
    return {
        "ok": r_ok and c_ok and h_ok,
        "route_ok": r_ok,
        "cards_ok": c_ok,
        "honest_ok": h_ok,
        "deterministic": _DETERMINISTIC[0] if _DETERMINISTIC else None,
        "first_tool": first,
        "tools": tools,
        "queries": queries,
        "cards": adapter.cards,
        "resp": resp[:200],
        "lat_ms": lat_ms,
        "error": None,
    }


async def run_e2e(cases: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    from app.infrastructure.repositories.brand_node_cache import warm_cache
    from app.providers import db_pool

    await db_pool.init_pool()
    await warm_cache()
    _wrap_deterministic_routers()

    out: dict[str, Any] = {}
    base_cid = 960000000
    try:
        for ai, arm in enumerate(arms):
            _set_models(*ARMS[arm])
            rows: list[dict[str, Any]] = []
            for ci, c in enumerate(cases):
                r = await _run_case(c, base_cid + ai * 100000 + ci)
                rows.append({"id": c["id"], "category": c["category"], "input": c["input"], **r})
                mark = "✅" if r["ok"] else "❌"
                det = f" det={r.get('deterministic')}" if r.get("deterministic") else ""
                print(
                    f"  {mark} {arm} [{c['category']:9s}] {c['id']:16s} tool={r.get('first_tool')} "
                    f"route={r.get('route_ok')} cards={r.get('cards_ok')} honest={r.get('honest_ok')}{det} "
                    f"{r.get('error') or ''}",
                    flush=True,
                )
            llm_rows = [x for x in rows if not x.get("deterministic")]
            lats = sorted(x["lat_ms"] for x in rows)
            out[arm] = {
                "success_pct": _pct([x["ok"] for x in rows]),
                "route_pct": _pct([bool(x.get("route_ok")) for x in rows]),
                "llm_routed_success_pct": _pct([x["ok"] for x in llm_rows]),
                "n_llm_routed": len(llm_rows),
                "n_deterministic": len(rows) - len(llm_rows),
                "by_cat": _by_cat([(x["category"], x["ok"]) for x in rows]),
                "lat_p50": statistics.median(lats) if lats else 0,
                "lat_p95": lats[int(len(lats) * 0.95)] if lats else 0,
                "cases": rows,
            }
            s = out[arm]
            print(
                f">>> {arm}: 성공 {s['success_pct']}% · 첫도구 {s['route_pct']}% · "
                f"LLM이 라우팅한 {s['n_llm_routed']}건만 {s['llm_routed_success_pct']}% · {s['by_cat']}",
                flush=True,
            )
    finally:
        _set_models(*ARMS["B_sonnet_router"])  # 운영 설정으로 원복
    return out


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["isolated", "e2e"], required=True)
    p.add_argument("--runs", type=int, default=3, help="isolated 반복 횟수")
    p.add_argument("--arms", default=",".join(ARMS), help="e2e arm 목록(콤마)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sha", default=None, help="측정 대상 커밋(컨테이너엔 .git 이 없음)")
    p.add_argument("--output", default=None)
    a = p.parse_args()

    cases = load_cases(a.limit)
    if a.mode == "isolated":
        res = asyncio.run(run_isolated(cases, a.runs))
    else:
        res = asyncio.run(run_e2e(cases, [x for x in a.arms.split(",") if x]))
    doc = {
        "mode": a.mode,
        "sha": a.sha or _git_sha(),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_cases": len(cases),
        "runs": a.runs if a.mode == "isolated" else 1,
        "results": res,
    }
    if a.output:
        Path(a.output).write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        print(f"[saved] {a.output}")


if __name__ == "__main__":
    main()
