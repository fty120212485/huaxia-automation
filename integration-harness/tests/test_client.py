import pytest
import httpx

from integration_harness.client import ApiError, HxaccClient
from integration_harness.config import load_config


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = b""

    def json(self):
        import json

        return json.loads(self.text)


def test_get_video_bytes_preserves_401_status(monkeypatch):
    config = load_config(
        {
            "HXACC_TOKEN": "secret-token",
            "HXACC_DEVICE_ID": "device-id",
        }
    )
    client = HxaccClient(config)
    monkeypatch.setattr(
        client.client,
        "get",
        lambda url, headers=None: _FakeResponse(401, "unauthorized"),
    )

    with pytest.raises(ApiError) as exc_info:
        client.get_video_bytes("https://example.com/video.ts", retries=1)

    assert exc_info.value.status_code == 401
    client.close()


def test_exam_mutation_is_not_automatically_retried(monkeypatch):
    config = load_config({"HXACC_TOKEN": "secret-token", "HXACC_DEVICE_ID": "device-id"})
    client = HxaccClient(config)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr(client.client, "post", post)
    try:
        with pytest.raises(ApiError, match="需查询服务端状态"):
            client.post_exam_json("postStartExam", {"examId": "exam"})
        assert len(calls) == 1
        assert calls[0][0] == f"{config.exam_base_url}/api/newexam/postStartExam"
        assert "sid=secret-token" in calls[0][1]["headers"]["Cookie"]
    finally:
        client.close()


def test_post_learn_json_retries_transient_network_error(monkeypatch):
    config = load_config(
        {
            "HXACC_TOKEN": "secret-token",
            "HXACC_DEVICE_ID": "device-id",
        }
    )
    client = HxaccClient(config)

    calls = {"count": 0}

    def fake_post(url, json=None, headers=None):
        calls["count"] += 1
        if calls["count"] < 3:
            raise httpx.ReadTimeout("read timed out")
        return _FakeResponse(200, '{"data": {}}')

    monkeypatch.setattr(client.client, "post", fake_post)

    assert client.post_learn_json("/api/foo", {}) == {"data": {}}
    assert calls["count"] == 3
    client.close()


def test_post_learn_json_raises_after_network_retries(monkeypatch):
    config = load_config(
        {
            "HXACC_TOKEN": "secret-token",
            "HXACC_DEVICE_ID": "device-id",
        }
    )
    client = HxaccClient(config)

    def fake_post(url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client.client, "post", fake_post)

    with pytest.raises(ApiError):
        client.post_learn_json("/api/foo", {})
    client.close()
