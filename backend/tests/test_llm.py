import pytest

from app.agent import llm, openrouter


def test_default_model_is_deepseek(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("MEDASSIST_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEDASSIST_OPENROUTER_MODEL", raising=False)
    config = Settings()
    assert config.llm_provider == "openrouter"
    assert config.openrouter_model == "deepseek/deepseek-v4.1-flash-1017"


def test_app_routes_chat_and_json_to_deepseek_without_ollama(monkeypatch):
    from app.agent import ollama
    requests = []

    def forbidden(*args, **kwargs):
        pytest.fail("Application attempted to use Ollama")

    def post(body):
        requests.append(body)
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(ollama, "chat", forbidden)
    monkeypatch.setattr(ollama, "chat_json", forbidden)
    monkeypatch.setattr(openrouter, "_post", post)
    assert llm.provider_name() == "openrouter"
    assert llm.chat([{"role": "user", "content": "Synthetic check"}])["content"] == '{"ok":true}'
    assert llm.chat_json("Synthetic check", "Return JSON") == {"ok": True}
    assert all(r["model"] == "deepseek/deepseek-v4.1-flash-1017" for r in requests)


def test_deepseek_failure_does_not_fall_back_to_ollama(monkeypatch):
    from app.agent import ollama

    def unavailable(*args, **kwargs):
        raise llm.ModelUnavailable("OpenRouter unavailable")

    monkeypatch.setattr(openrouter, "chat", unavailable)
    monkeypatch.setattr(ollama, "chat", lambda *a, **kw: pytest.fail("Unexpected Ollama fallback"))
    with pytest.raises(llm.ModelUnavailable, match="OpenRouter unavailable"):
        llm.chat([{"role": "user", "content": "Synthetic check"}])


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
