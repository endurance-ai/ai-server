"""SPEC-CONVERSATION-LOG-001 / LOG-T23 — REQ-LOG-CATALOG-001 AST enumeration.

이 테스트는 두 가지 카탈로그 invariant 를 잠근다:

(a) `app.observability.event_payloads.__all__` 의 길이가 정확히 **19** 이다.
    SPEC §Event Type Catalog 와 1:1 대응. 새 이벤트 추가시 SPEC + `__all__` +
    본 테스트가 동시에 갱신되어야 한다.

(b) `TasteSource` Literal 의 7-값(`click` / `onboard` / `pinterest` / `critique` /
    `free_text` / `no_click` / `re_query`) 가 실제 `emit(...) payload={"source": "<val>"}`
    호출 site 에 ≥ 1 회 등장한다. AST scan target:
        - `app/graphs/nodes/*.py`
        - `app/channels/implicit_feedback.py`

    예외:
        - `onboard` — SPEC-ONBOARD-CARDS-001 미구현 (xfail).
        - `pinterest` — SPEC-ONBOARD-CARDS-001 pinterest 단계 미구현 (xfail).

@MX:SPEC: SPEC-CONVERSATION-LOG-001
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GRAPH_NODES_DIR = REPO_ROOT / "app" / "graphs" / "nodes"
IMPLICIT_FB_PATH = REPO_ROOT / "app" / "channels" / "implicit_feedback.py"


# These tests are pure AST — do not require Docker. Override the package-level
# autouse `_truncate_log_table` fixture (defined in conftest.py) which would
# otherwise pull in `pool_initialized → pg_dsn → pg_container` and skip when
# Docker is absent.
@pytest.fixture(autouse=True)
def _truncate_log_table():
    yield


# ───────────────────────── (a) `__all__` length ─────────────────────────


def test_event_payloads_all_length_is_19():
    """REQ-LOG-CATALOG-001 — 19 → 20 event types after SPEC-AGENT-V2-REACT (tool_call added)."""
    mod = importlib.import_module("app.observability.event_payloads")
    assert len(mod.__all__) == 20, f"__all__ length must be 20, got {len(mod.__all__)}"


def test_event_payloads_star_import_exposes_19_typeddicts():
    """`from app.observability.event_payloads import *` publishes exactly 20 names (post-V2-REACT)."""
    ns: dict = {}
    exec("from app.observability.event_payloads import *", ns)  # noqa: S102
    public = {k for k in ns if not k.startswith("_")}
    assert len(public) == 20, f"star-import exposed {len(public)} names, expected 20"


# ───────────────────── (b) taste_update.source AST scan ─────────────────


def _scan_emit_taste_update_sources(file_path: Path) -> set[str]:
    """Return the set of `source` string-Literal values used in `emit(...)` calls
    whose payload event_type is `taste_update` within the given file.

    Pattern recognized:
        emit(
            event_type="taste_update",
            ...
            payload={"source": "click", ...},
        )
    Or where event_type is a kwarg and payload literal-dict contains a `source` key.
    Aliased imports (e.g. `from .conversation_log import emit as _conv_emit`) are
    detected by matching any Call whose `func` is a `Name` ending with `emit`.
    """
    if not file_path.exists():
        return set()
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    sources: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Accept `emit(...)` and any `<alias>emit(...)` (e.g. `_conv_emit`).
        if isinstance(func, ast.Name) and func.id.endswith("emit"):
            pass
        elif isinstance(func, ast.Attribute) and func.attr.endswith("emit"):
            pass
        else:
            continue
        # Find event_type kwarg.
        kw_map = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        et = kw_map.get("event_type")
        if not isinstance(et, ast.Constant) or et.value != "taste_update":
            continue
        payload = kw_map.get("payload")
        if not isinstance(payload, ast.Dict):
            continue
        for k, v in zip(payload.keys, payload.values, strict=False):
            if (
                isinstance(k, ast.Constant)
                and k.value == "source"
                and isinstance(v, ast.Constant)
                and isinstance(v.value, str)
            ):
                sources.add(v.value)
    return sources


def _collect_all_taste_sources() -> set[str]:
    """Aggregate `taste_update.source` Literal values across all in-scope files."""
    files = sorted(GRAPH_NODES_DIR.glob("*.py")) + [IMPLICIT_FB_PATH]
    found: set[str] = set()
    for path in files:
        found |= _scan_emit_taste_update_sources(path)
    return found


# Currently-emittable sources (7/7) — SPEC-ONBOARD-CARDS-001 landed Phase 4
# (ONB-T22), wiring `taste_update.source="onboard"` in `_onboard_helpers.py`
# and `source="pinterest"` in `_pinterest_helpers.py`. The xfail bookmark for
# unimplemented sources is retired.
_IMPLEMENTED_SOURCES = {
    "click",
    "critique",
    "free_text",
    "no_click",
    "onboard",
    "pinterest",
    "re_query",
}
_UNIMPLEMENTED_SOURCES: set[str] = set()


@pytest.mark.parametrize("source", sorted(_IMPLEMENTED_SOURCES))
def test_taste_update_implemented_source_has_emit_site(source: str):
    """REQ-LOG-CATALOG-001 — each implemented `taste_update.source` value
    appears in ≥ 1 `emit(...)` site across graph nodes or implicit_feedback."""
    found = _collect_all_taste_sources()
    assert source in found, (
        f"taste_update.source={source!r} not emitted from any in-scope file. Currently found: {sorted(found)}"
    )


def test_all_seven_sources_covered_by_test_matrix():
    """Meta-check: the two parametrize sets together cover all 7 TasteSource values."""
    from app.observability.event_payloads import TasteSource

    # TasteSource is `Literal[...]`; extract args.
    literal_values = set(TasteSource.__args__)
    assert literal_values == _IMPLEMENTED_SOURCES | _UNIMPLEMENTED_SOURCES, (
        f"TasteSource Literal values {literal_values} drifted from test matrix "
        f"{_IMPLEMENTED_SOURCES | _UNIMPLEMENTED_SOURCES}"
    )
