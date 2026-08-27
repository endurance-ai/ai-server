from app.providers.litellm_chat import LiteLLMChatOpenAI


def test_preserves_raw_usage_and_response_headers():
    client = LiteLLMChatOpenAI(model="test-model", api_key="test-key")
    response = {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 200,
        },
    }

    result = client._create_chat_result(
        response,
        {"headers": {"x-litellm-response-cost": "0.123", "x-litellm-call-id": "call-1"}},
    )

    message = result.generations[0].message
    assert message.response_metadata["raw_usage"]["cache_creation_input_tokens"] == 200
    assert message.response_metadata["headers"]["x-litellm-response-cost"] == "0.123"
    assert message.usage_metadata == {
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "input_token_details": {},
        "output_token_details": {},
    }
