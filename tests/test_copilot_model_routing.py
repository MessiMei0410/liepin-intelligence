from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import pytest

from a_system_agent.config import DEFAULTS
from a_system_agent.llm import (
    LLMError,
    OpenAICompatibleLLM,
    classify_copilot_route,
    create_default_llm,
    parse_copilot_tool_response,
)


def _provider(model: str) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=f"https://{model}.example/v1",
        api_key=f"{model}-test-key",
        model=model,
        retry_attempts=1,
    )


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        (
            {
                "question": "继续说说这个岗位",
                "conversation": {
                    "recent_history": [{"role": "user", "content": "先看岗位要求"}],
                },
            },
            "multi_turn",
        ),
        ({"question": "更正一下，预算是 120w"}, "correction"),
        ({"question": "详细解释为什么匹配", "response_detail": "expanded"}, "detailed"),
        (
            {
                "question": "帮我整理谈薪方案",
                "intent_understanding": {
                    "action": "salary",
                    "action_evidence": ["帮我整理谈薪方案"],
                },
                "turn_decision": {"effect": "create_plan", "safe_for_action": True},
            },
            "sensitive_action",
        ),
        ({"current_message": "可以", "known_targets": []}, "ambiguous"),
    ],
)
def test_complex_copilot_turns_route_to_strong_model(
    payload: dict,
    expected_reason: str,
) -> None:
    route = classify_copilot_route(payload)
    assert route["tier"] == "strong"
    assert expected_reason in route["reasons"]


def test_standalone_budget_fact_remains_on_fast_model() -> None:
    route = classify_copilot_route({
        "current_message": "士兰微这个岗位预算 120w",
        "recent_user_messages": [],
        "known_targets": [{"type": "job", "id": 10}],
        "deterministic_hint": "none",
    })
    assert route == {"tier": "fast", "reasons": []}


def test_clear_budget_fact_is_fast_even_when_other_targets_are_known() -> None:
    route = classify_copilot_route({
        "current_message": "长越科技这个岗位预算 120w",
        "current_context": {"type": "job", "id": 137},
        "known_targets": [
            {"type": "job", "id": 137},
            {"type": "job", "id": 138},
        ],
        "deterministic_hint": "none",
    })
    assert route == {"tier": "fast", "reasons": []}


def test_simple_multi_turn_observation_stays_fast() -> None:
    route = classify_copilot_route({
        "current_message": "这轮只找到两个人选",
        "recent_user_messages": ["给这个岗位补充候选人"],
        "deterministic_hint": "none",
        "intent_understanding": {"action": "none", "action_evidence": []},
    })
    assert route == {"tier": "fast", "reasons": []}


def test_observation_words_do_not_preserve_a_false_sourcing_hint() -> None:
    route = classify_copilot_route({
        "current_message": "这轮只找到两个人选",
        "recent_user_messages": ["给这个岗位补充候选人"],
        "deterministic_hint": "candidate_sourcing",
        "intent_understanding": {"action": "none", "action_evidence": []},
    })
    assert route == {"tier": "fast", "reasons": []}


def test_explicit_follow_up_search_with_the_same_words_stays_sensitive() -> None:
    route = classify_copilot_route({
        "current_message": "继续再找一些人选",
        "recent_user_messages": ["给这个岗位补充候选人"],
        "deterministic_hint": "candidate_sourcing",
        "intent_understanding": {"action": "candidate_sourcing", "action_evidence": ["继续再找一些人选"]},
    })
    assert route["tier"] == "strong"
    assert "sensitive_action" in route["reasons"]


def test_routed_provider_uses_strong_model_for_intent_and_tool_answer() -> None:
    fast = _provider("deepseek-v4-flash")
    strong = _provider("gpt-5.5")
    fast.strong_copilot_llm = strong
    fast_request = Mock(return_value='{"speech_act":"inform","action":"none"}')
    strong_request = Mock(return_value='{"speech_act":"correct","action":"none"}')
    fast._request = fast_request
    strong._request = strong_request

    intent = fast.interpret_copilot_intent({"current_message": "更正一下，是 120w"})
    assert intent == {"speech_act": "correct", "action": "none"}
    strong_request.assert_called_once()
    fast_request.assert_not_called()

    fast_tools = Mock(return_value={"content": "fast", "tool_calls": []})
    strong_tools = Mock(return_value={"content": "strong", "tool_calls": []})
    fast._copilot_with_tools_local = fast_tools
    strong._copilot_with_tools_local = strong_tools
    result = fast.copilot_with_tools(
        {"question": "详细解释这个岗位", "response_detail": "expanded"},
        [],
    )
    assert result["content"] == "strong"
    strong_tools.assert_called_once()
    fast_tools.assert_not_called()
    assert fast.copilot_runtime_metadata() == {
        "model": "gpt-5.5",
        "tier": "strong",
        "requested_tier": "strong",
        "reasons": ["detailed"],
        "fallback_used": False,
    }


def test_strong_model_failure_falls_back_to_fast_model() -> None:
    fast = _provider("deepseek-v4-flash")
    strong = _provider("gpt-5.5")
    fast.strong_copilot_llm = strong
    fast._request = Mock(return_value="fast answer")
    strong._request = Mock(side_effect=LLMError("strong unavailable"))

    answer = fast.copilot({"question": "请详细分析", "response_detail": "expanded"})
    assert answer == "fast answer"
    assert fast.copilot_runtime_metadata() == {
        "model": "deepseek-v4-flash",
        "tier": "fast",
        "requested_tier": "strong",
        "reasons": ["detailed"],
        "fallback_used": True,
    }

    second = fast.copilot({"question": "请详细分析", "response_detail": "expanded"})
    assert second == "fast answer"
    assert strong._request.call_count == 1
    assert "strong_circuit_open" in fast.copilot_runtime_metadata()["reasons"]


def test_default_factory_builds_optional_strong_model_without_exposing_keys(monkeypatch) -> None:
    config = deepcopy(DEFAULTS)
    monkeypatch.setenv("A_SYSTEM_AGENT_API_KEY", "fast-secret")
    monkeypatch.setenv("A_SYSTEM_AGENT_COPILOT_STRONG_API_KEY", "strong-secret")

    llm = create_default_llm(config)
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.model == "deepseek-v4-flash"
    assert llm.has_strong_copilot_model() is True
    assert llm.strong_copilot_llm is not None
    assert llm.strong_copilot_llm.model == "gpt-5.5"
    assert "secret" not in str(llm.copilot_runtime_metadata())


def test_default_factory_can_disable_strong_model(monkeypatch) -> None:
    config = deepcopy(DEFAULTS)
    config["copilot_routing"]["enabled"] = False
    monkeypatch.setenv("A_SYSTEM_AGENT_API_KEY", "fast-secret")
    monkeypatch.delenv("A_SYSTEM_AGENT_COPILOT_STRONG_API_KEY", raising=False)

    llm = create_default_llm(config)
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.has_strong_copilot_model() is False


def test_deepseek_dsml_tool_calls_are_normalized_and_removed_from_content() -> None:
    response = parse_copilot_tool_response(
        {
            "content": (
                "候选人详情接口报错了。让我换个方式。\n\n"
                "<｜｜DSML｜｜tool_calls>\n"
                "<｜｜DSML｜｜invoke name=\"get_candidate_assessment\">\n"
                "<｜｜DSML｜｜parameter name=\"job_id\" string=\"false\">137</｜｜DSML｜｜parameter>\n"
                "<｜｜DSML｜｜parameter name=\"limit\" string=\"false\">20</｜｜DSML｜｜parameter>\n"
                "</｜｜DSML｜｜invoke>\n"
                "</｜｜DSML｜｜tool_calls>"
            )
        }
    )

    assert response["content"] == "候选人详情接口报错了。让我换个方式。"
    assert "DSML" not in response["content"]
    assert response["tool_calls"] == [
        {
            "id": "dsml_tool_0",
            "name": "get_candidate_assessment",
            "arguments": {"job_id": 137, "limit": 20},
        }
    ]


def test_tool_response_keeps_native_calls_and_supports_deepseek_token_format() -> None:
    response = parse_copilot_tool_response(
        {
            "content": (
                "<｜tool▁calls▁begin｜>"
                "<｜tool▁call▁begin｜>functions.get_dashboard"
                "<｜tool▁sep｜>{}<｜tool▁call▁end｜>"
                "<｜tool▁calls▁end｜>"
            ),
            "tool_calls": [
                {
                    "id": "native_1",
                    "function": {"name": "query_job", "arguments": '{"job_id":137}'},
                }
            ],
        }
    )

    assert response["content"] == ""
    assert response["tool_calls"] == [
        {
            "id": "native_1",
            "name": "query_job",
            "arguments": {"job_id": 137},
        },
        {"id": "dsml_tool_1", "name": "get_dashboard", "arguments": {}},
    ]


def test_truncated_tool_protocol_is_removed_instead_of_being_shown() -> None:
    response = parse_copilot_tool_response(
        {
            "content": (
                "先给结论。\n<｜｜DSML｜｜tool_calls>"
                "<｜｜DSML｜｜invoke name=\"query_job\">"
                "<｜｜DSML｜｜parameter name=\"job_id\" string=\"false\">137"
            )
        }
    )

    assert response["content"] == "先给结论。"
    assert "DSML" not in response["content"]
    assert response["tool_calls"] == []


def test_openai_compatible_tool_request_uses_normalized_content(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "自然语言前缀\n"
                                    "<｜｜DSML｜｜tool_calls>"
                                    "<｜｜DSML｜｜invoke name=\"query_job\">"
                                    "<｜｜DSML｜｜parameter name=\"job_id\" string=\"false\">137"
                                    "</｜｜DSML｜｜parameter>"
                                    "</｜｜DSML｜｜invoke>"
                                    "</｜｜DSML｜｜tool_calls>"
                                )
                            }
                        }
                    ]
                }
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    llm = _provider("deepseek-v4-flash")
    result = llm._copilot_with_tools_local({}, [])

    assert result["content"] == "自然语言前缀"
    assert result["tool_calls"][0]["name"] == "query_job"
    assert result["tool_calls"][0]["arguments"] == {"job_id": 137}
    assert "DSML" not in result["content"]
