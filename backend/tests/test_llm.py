import pytest

from app.agent import llm, openrouter


def test_tool_messages_convert_to_openai_format():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "function": {"name": "read_table", "arguments": {"dataset_id": "ds_1"}}},
            {"function": {"name": "create_artifact", "arguments": {"dataset_id": "ds_1", "format": "xlsx"}}},
        ]},
        {"role": "tool", "content": "{}", "tool_name": "read_table", "tool_call_id": "call_a"},
        {"role": "tool", "content": "{}", "tool_name": "create_artifact"},
    ]
    out = openrouter._to_openai(msgs)
    calls = out[2]["tool_calls"]
    assert calls[0]["function"]["arguments"] == '{"dataset_id": "ds_1"}'
    assert calls[1]["id"].startswith("call_")
    assert out[3]["tool_call_id"] == "call_a"
    assert out[4]["tool_call_id"] == calls[1]["id"]  # id assigned to an unnamed call is reused for its result


def test_cloud_provider_requires_explicit_opt_in(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "allow_cloud_model", property(lambda self: False), raising=False)
    with llm.use("openrouter"):
        with pytest.raises(llm.ModelUnavailable):
            llm.chat_json("s", "u")
        assert llm.status()["ok"] is False
