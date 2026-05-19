from datetime import datetime

import pytest

from app.graphs.nodes._first_touch import maybe_first_touch


class _Adapter:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append(text)


class _Msg:
    def __init__(self, text=None, photo_file_id=None, urls=None, callback_data=None):
        self.text = text
        self.photo_file_id = photo_file_id
        self.urls = urls or []
        self.callback_data = callback_data


class _State:
    def __init__(self, msg, chat_id=1, from_user_id=7):
        self.message = msg
        self.chat_id = chat_id
        self.from_user_id = from_user_id
        self.thread_id = None
        self.turn_no = 1


class _Sess:
    def __init__(self, onboarded_at=None):
        self.onboarded_at = onboarded_at
        self.lang = "ko"


@pytest.mark.asyncio
async def test_new_user_actionable_photo_greets_and_marks():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=None)
    state = _State(_Msg(photo_file_id="abc"))
    deleted = []
    await maybe_first_touch(
        state,
        sess,
        adapter,
        taste_delete=lambda uk: deleted.append(uk),
        breadcrumbs=[],
    )
    assert any("kiko" in s for s in adapter.sent)
    assert isinstance(sess.onboarded_at, datetime)
    assert deleted == []


@pytest.mark.asyncio
async def test_new_user_start_only_no_greeting_no_mark():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=None)
    state = _State(_Msg(text="/start"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert adapter.sent == []  # intro 노드가 처리 — ingest는 침묵
    assert sess.onboarded_at is None  # intro 노드가 마킹


@pytest.mark.asyncio
async def test_reset_keyword_clears_taste_and_acks():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="/reset"))
    deleted = []
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: deleted.append(uk), breadcrumbs=[])
    assert deleted == ["u:7"]
    assert any("초기화" in s or "reset" in s.lower() for s in adapter.sent)


@pytest.mark.asyncio
async def test_returning_user_actionable_no_greeting():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="미니멀 코트"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_returning_user_start_only_acks():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="/start"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert len(adapter.sent) == 1  # 가벼운 ready ack
