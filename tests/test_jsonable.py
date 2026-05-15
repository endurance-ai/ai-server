"""DDD PRESERVE — characterization tests for `_to_jsonable` 5-step cascade.

Captures the *current* behavior of `app.channels.session_pg._to_jsonable` BEFORE
extraction to `app.channels._jsonable`. After extraction, both import paths
MUST produce byte-identical results for the same input set (this file imports
from the new location; pre-extraction it imports from session_pg).

REQ: resolves plan §1.3 OQ-1 (LOG-T03 characterization gate).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.channels._jsonable import to_jsonable

# ── Pydantic v2 ────────────────────────────────────────────────────────────


class _Sample(BaseModel):
    name: str
    n: int


def test_pydantic_v2_model_dump():
    out = to_jsonable(_Sample(name="x", n=3))
    assert out == {"name": "x", "n": 3}


# ── Dataclass ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _DCSample:
    name: str
    n: int


def test_dataclass_asdict():
    out = to_jsonable(_DCSample(name="x", n=3))
    assert out == {"name": "x", "n": 3}


# ── Primitives ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["hello", 42, 3.14, True, False],
)
def test_primitives_passthrough(value):
    assert to_jsonable(value) == value


def test_none_returns_none():
    assert to_jsonable(None) is None


# ── Datetime / UUID / Path — fall through to json default=str ──────────────


def test_datetime_coerced_via_default_str():
    dt = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    out = to_jsonable(dt)
    # 묵시적 default=str cascade — ISO 형식 문자열
    assert isinstance(out, str)
    assert "2026-05-14" in out


def test_uuid_coerced_via_default_str():
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    out = to_jsonable(u)
    assert out == "12345678-1234-5678-1234-567812345678"


def test_path_coerced_via_default_str():
    p = Path("/tmp/example.txt")
    out = to_jsonable(p)
    assert isinstance(out, str)
    assert "example.txt" in out


# ── Nested structures ──────────────────────────────────────────────────────


def test_list_of_mixed():
    out = to_jsonable([1, "a", None, _Sample(name="y", n=1)])
    assert out == [1, "a", None, {"name": "y", "n": 1}]


def test_tuple_becomes_list():
    out = to_jsonable((1, 2, 3))
    assert out == [1, 2, 3]


def test_dict_nested():
    out = to_jsonable({"k": _DCSample(name="z", n=2), "arr": [1, 2]})
    assert out == {"k": {"name": "z", "n": 2}, "arr": [1, 2]}


def test_deep_nesting():
    payload = {"a": [{"b": _Sample(name="w", n=0)}]}
    out = to_jsonable(payload)
    assert out == {"a": [{"b": {"name": "w", "n": 0}}]}
