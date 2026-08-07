from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent.llm import LLMError, OpenAICompatibleLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.service import CoreService  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _db() -> tuple[tempfile.TemporaryDirectory, Path]:
    temp = tempfile.TemporaryDirectory()
    path = Path(temp.name) / "audit.db"
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    conn.close()
    return temp, path


def test_model_call_audit_tracks_validation_tokens_and_redacts_payload() -> None:
    temp, db_path = _db()
    try:
        llm = OpenAICompatibleLLM(
            base_url="https://api.deepseek.com/v1",
            api_key="secret-key",
            model="deepseek-v4-flash",
            db_path=db_path,
            retry_attempts=1,
        )
        response = {
            "choices": [{"message": {"content": '{"trajectory":{"verdict":"ok"}}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)):
            assert llm.assess_trajectory({"candidate": {"resume": "SECRET_RESUME"}})["trajectory"]["verdict"] == "ok"

        audit = CoreService(db_path).model_audit()
        assert audit["summary"]["total"] == 1
        item = audit["items"][0]
        assert item["operation"] == "assess_trajectory"
        assert item["status"] == "success"
        assert item["validation_status"] == "passed"
        assert item["input_tokens"] == 11 and item["output_tokens"] == 7
        assert "SECRET_RESUME" not in item["request_preview"]
        assert "secret-key" not in json.dumps(item)
    finally:
        temp.cleanup()


def test_invalid_structured_output_can_be_marked_as_rule_fallback() -> None:
    temp, db_path = _db()
    try:
        llm = OpenAICompatibleLLM(
            base_url="https://api.deepseek.com/v1",
            api_key="secret-key",
            model="deepseek-v4-flash",
            db_path=db_path,
            retry_attempts=1,
        )
        response = {"choices": [{"message": {"content": "not-json"}}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)):
            with pytest.raises(LLMError):
                llm.assess_risks({"candidate": {"resume": "hidden"}})
        llm.mark_last_call_fallback()

        item = CoreService(db_path).model_audit()["items"][0]
        assert item["status"] == "failed"
        assert item["validation_status"] == "failed"
        assert item["fallback_used"] == 1
    finally:
        temp.cleanup()
