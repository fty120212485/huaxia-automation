import os
from pathlib import Path

import pytest

from integration_harness.config import Config, load_config


def test_load_config_requires_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HXACC_TOKEN", raising=False)
    monkeypatch.setenv("HXACC_DEVICE_ID", "device-id")
    with pytest.raises(RuntimeError, match="HXACC_TOKEN is required"):
        load_config()


def test_load_config_requires_device_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HXACC_TOKEN", "secret-token")
    monkeypatch.delenv("HXACC_DEVICE_ID", raising=False)
    with pytest.raises(RuntimeError, match="HXACC_DEVICE_ID is required"):
        load_config()


def test_load_config_reads_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTO_NEXT_COURSE", raising=False)
    (tmp_path / ".env").write_text(
        "HXACC_TOKEN=secret-token\n"
        "HXACC_DEVICE_ID=device-id\n",
        encoding="utf-8",
    )
    config = load_config()
    assert config.token == "secret-token"
    assert config.token_prefix == "secret-token"
    assert config.device_id == "device-id"
    assert config.video_quality == "FD"
    assert config.auto_next_course is False


def test_load_config_reads_auto_next_course_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HXACC_TOKEN", "secret-token")
    monkeypatch.setenv("HXACC_DEVICE_ID", "device-id")
    monkeypatch.setenv("AUTO_NEXT_COURSE", "true")

    config = load_config()

    assert config.auto_next_course is True


def test_config_runs_dir_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HXACC_TOKEN", "secret-token")
    monkeypatch.setenv("HXACC_DEVICE_ID", "device-id")
    monkeypatch.setenv("RUNS_DIR", "runs")
    config = load_config()
    assert config.runs_dir == tmp_path / "runs"


def test_exam_configuration_and_secret_repr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config({
        "HXACC_TOKEN": "test-token", "HXACC_DEVICE_ID": "test-device",
        "EXAM_BASE_URL": "https://exam.test/", "EXAM_TASK_MAP_JSON": '{"task":["exam"]}',
        "EXAM_HEARTBEAT_SECONDS": "10", "EXAM_POLL_ATTEMPTS": "5",
        "LLM_API_KEY": "private-llm-key", "LLM_MODEL": "test-model",
    })
    assert config.exam_base_url == "https://exam.test"
    assert config.exam_task_map_json == '{"task":["exam"]}'
    assert config.exam_heartbeat_seconds == 10
    assert config.exam_poll_attempts == 5
    assert config.llm_model == "test-model"
    assert config.llm_api_key == "private-llm-key"
    assert "private-llm-key" not in repr(config)


def test_load_config_cli_overrides_env_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HXACC_TOKEN", "env-token")
    monkeypatch.setenv("HXACC_DEVICE_ID", "env-device")
    monkeypatch.setenv("APP_ID", "env-app")

    config = load_config(
        {
            "HXACC_TOKEN": "cli-token",
            "HXACC_DEVICE_ID": "cli-device",
            "APP_ID": "cli-app",
        }
    )

    assert config.token == "cli-token"
    assert config.device_id == "cli-device"
    assert config.app_id == "cli-app"
