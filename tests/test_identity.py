from datetime import UTC, datetime
from uuid import UUID

from app.channels.schemas import ChannelMessage
from app.core.identity import user_id_to_session_key
from app.graphs.state import InputState
from app.infrastructure.memory.session import Session
from app.services.chat_service import _user_id_to_chat_id


def test_user_identity_maps_to_stable_bounded_session_key() -> None:
    user_id = UUID("12345678-1234-5678-1234-567812345678")

    key = user_id_to_session_key(user_id)

    assert key == user_id_to_session_key(user_id)
    assert 0 <= key < 2**62


def test_legacy_chat_id_helper_remains_a_behavior_compatible_alias() -> None:
    user_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    assert _user_id_to_chat_id(user_id) == user_id_to_session_key(user_id)


def test_session_key_is_canonical_with_chat_id_input_compatibility() -> None:
    message = ChannelMessage(chat_id=7, received_at=datetime.now(UTC))
    state = InputState(message=message, chat_id=7)

    assert message.session_key == message.chat_id == 7
    assert state.session_key == state.chat_id == 7
    assert "chat_id" in message.model_dump()
    assert "chat_id" in state.model_dump()

    # New channel-neutral spelling is accepted without changing the wire/model
    # field used by existing graph integrations.
    assert ChannelMessage(session_key=8, received_at=datetime.now(UTC)).session_key == 8
    assert InputState(message=message, session_key=8).session_key == 8

    session = Session(chat_id=9)
    assert session.session_key == 9
    session.session_key = 10
    assert session.chat_id == 10
