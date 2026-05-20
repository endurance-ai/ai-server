from datetime import datetime

import pytest

from app.graphs import fashion_bot
from app.graphs.fashion_bot import build_graph
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    get_store,
    set_store,
)


class _Msg:
    def __init__(self, text=None, photo_file_id=None, urls=None, callback_data=None, callback_query_id=None):
        self.text = text
        self.photo_file_id = photo_file_id
        self.urls = urls or []
        self.callback_data = callback_data
        self.callback_query_id = callback_query_id


class _State:
    def __init__(self, msg, chat_id):
        self.message = msg
        self.chat_id = chat_id
        self.from_user_id = 7
        self.selected_item_index = None


@pytest.fixture(autouse=True)
def _fresh_store():
    set_store(InMemorySessionStore())
    yield
    set_store(InMemorySessionStore())


def _router():
    # _route_after_ingest_v2 is a closure built inside build_graph; the
    # module-level seam binds the last-built instance.
    return fashion_bot._route_after_ingest_v2


def test_new_user_start_only_routes_to_intro():
    s = get_store().get_or_create(100)
    assert s.onboarded_at is None
    assert _router()(_State(_Msg(text="/start"), 100)) == "intro"


def test_new_user_photo_routes_to_resolve_image():
    get_store().get_or_create(101)
    assert _router()(_State(_Msg(photo_file_id="x"), 101)) == "resolve_image"


def test_new_user_text_routes_to_agent():
    get_store().get_or_create(102)
    assert _router()(_State(_Msg(text="미니멀 코트"), 102)) == "agent"


def test_reset_keyword_routes_to_end():
    s = get_store().get_or_create(103)
    s.onboarded_at = datetime.now()
    assert _router()(_State(_Msg(text="/reset"), 103)) == "__end__"


def test_contentless_routes_to_end():
    get_store().get_or_create(104)
    assert _router()(_State(_Msg(), 104)) == "__end__"


def test_returning_user_text_routes_to_agent():
    s = get_store().get_or_create(105)
    s.onboarded_at = datetime.now()
    assert _router()(_State(_Msg(text="안녕"), 105)) == "agent"


def test_graph_builds_without_onboarding_nodes():
    g = build_graph()
    assert g is not None  # no ImportError from removed onboard_* modules
