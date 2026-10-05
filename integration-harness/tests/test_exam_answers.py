import json
from types import SimpleNamespace

import httpx
import pytest

from integration_harness.exam_answers import LlmAnswerer


@pytest.mark.parametrize("model", ["model", "glm-4.7-flash"])
def test_llm_only_receives_question_and_own_credentials(monkeypatch, model):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "B"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr("integration_harness.exam_answers.httpx.Client", lambda **kw: client)
    config = SimpleNamespace(llm_api_key="llm-test-key", llm_base_url="https://llm.test",
                             llm_model=model, llm_timeout_seconds=60, llm_retries=1,
                             token="account-secret", device_id="device-secret")
    question = {"id": "q", "type": "single", "title": "题目",
                "options": [{"value": "A", "label": "甲"}, {"value": "B", "label": "乙"}]}
    assert LlmAnswerer(config)(question) == "B"
    payload = json.loads(requests[0].content)
    if model == "glm-4.7-flash":
        assert payload["thinking"] == {"type": "enabled"}
    else:
        assert "thinking" not in payload
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer llm-test-key"
    assert "Cookie" not in request.headers
    content = request.content.decode()
    assert "account-secret" not in content
    assert "device-secret" not in content
    assert json.loads(json.loads(content)["messages"][1]["content"]) == question


def test_invalid_llm_response_cannot_be_used_as_answer(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "答案 A"}}]})))
    monkeypatch.setattr("integration_harness.exam_answers.httpx.Client", lambda **kw: client)
    config = SimpleNamespace(llm_api_key="key", llm_base_url="https://llm.test", llm_model="model",
                             llm_timeout_seconds=60, llm_retries=1)
    with pytest.raises(ValueError, match="未获得有效"):
        LlmAnswerer(config)({"id": "q", "type": "single", "options": [{"value": "A", "label": "甲"}]})


@pytest.mark.parametrize("mode, expected", [
    ("http", "HTTP 401"), ("timeout", "ReadTimeout"),
    ("json", "响应解析失败"), ("empty", "finish_reason=length"),
    ("invalid", "答案校验失败"),
])
def test_failure_diagnostics_are_logged_and_redacted(monkeypatch, caplog, mode, expected):
    def handle(request):
        if mode == "http":
            return httpx.Response(401, text="invalid secret-test-key")
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "json":
            return httpx.Response(200, text="not json secret-test-key")
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length", "message": {
                "content": "" if mode == "empty" else "答案 A secret-test-key",
                "reasoning_content": "thinking",
            }}], "usage": {"completion_tokens": 2048}})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr("integration_harness.exam_answers.httpx.Client", lambda **kw: client)
    config = SimpleNamespace(llm_api_key="secret-test-key", llm_base_url="https://llm.test",
                             llm_model="model", llm_timeout_seconds=60, llm_retries=1)
    with pytest.raises(ValueError) as error:
        LlmAnswerer(config)({"id": "q", "type": "single", "options": [{"value": "A"}]})
    assert expected in str(error.value)
    assert expected in caplog.text
    assert "attempt=1/1" in caplog.text
    assert "secret-test-key" not in caplog.text + str(error.value)


def test_truncated_answer_retries_with_bounded_budget(monkeypatch):
    budgets = []
    def handle(request):
        payload = json.loads(request.content)
        budgets.append(payload["max_tokens"])
        assert payload["thinking"] == {"type": "enabled"}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length" if len(budgets) < 3 else "stop",
            "message": {"content": "A" if len(budgets) < 3 else "AB", "reasoning_content": "analysis"}
        }]})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr("integration_harness.exam_answers.httpx.Client", lambda **kw: client)
    monkeypatch.setattr("integration_harness.exam_answers.time.sleep", lambda _: None)
    config = SimpleNamespace(llm_api_key="key", llm_base_url="https://llm.test", llm_model="glm-4.7-flash",
                             llm_timeout_seconds=60, llm_retries=3, llm_max_tokens=8192,
                             llm_max_tokens_limit=12000)
    assert LlmAnswerer(config)({"id": "q", "type": "multiple",
                               "options": [{"value": "A"}, {"value": "B"}]}) == "AB"
    assert budgets == [8192, 12000, 12000]
