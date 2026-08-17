"""回归：「把他详情页拉起来」应识别为显式只读导航并绑定上一轮查询结果中的候选人。

缺陷背景（copilot_5e5c219a5682）：导航指令被识别为 action=none/target=global，
模型重复输出详情文字而不发出 open_candidate UI 动作。修复点：
1. 导航指令确定性识别（_candidate_navigation_requested）；
2. 查询工具结果/引用中的候选人回写会话候选人焦点（referenced_candidates）；
3. 导航回合产出带 auto 标记的 open_candidate 动作，前端自动打开详情面板。
"""

from __future__ import annotations

import json
import sqlite3

from a_system_agent import AgentService, FakeLLM
from a_system_agent.copilot_intent import _candidate_navigation_requested, _navigation_intent_kind
from test_a_system_agent_v1 import AgentDbCase, fake_assessment


def test_candidate_navigation_requested_positive() -> None:
    assert _candidate_navigation_requested("把他详情页拉起来")
    assert _candidate_navigation_requested("把人选的详情页打开")
    assert _candidate_navigation_requested("打开这个候选人的详情页")
    assert _candidate_navigation_requested("把TA的资料页调出来")


def test_navigation_intent_kind_job_and_workflow() -> None:
    assert _navigation_intent_kind("把这个岗位的详情页打开") == "job"
    assert _navigation_intent_kind("打开岗位详情页") == "job"
    assert _navigation_intent_kind("把职位详情拉出来") == "job"
    assert _navigation_intent_kind("把这个工作流打开") == "workflow"
    assert _navigation_intent_kind("打开工作流详情页") == "workflow"
    assert _navigation_intent_kind("查看当前工作流") == "workflow"
    # 人选优先于岗位/工作流；疑问句与无关指令不导航。
    assert _navigation_intent_kind("把人选的详情页打开") == "candidate"
    assert _navigation_intent_kind("他的详情页打开了吗") == ""
    assert _navigation_intent_kind("这个岗位现在多少人") == ""
    assert _navigation_intent_kind("开始执行工作流") == ""
    assert _navigation_intent_kind("") == ""


def test_candidate_navigation_requested_negative() -> None:
    # 岗位/对象级导航不命中人选导航。
    assert not _candidate_navigation_requested("打开岗位详情页")
    assert not _candidate_navigation_requested("把这个职位的详情页打开")
    # 疑问句是状态询问，不是导航指令。
    assert not _candidate_navigation_requested("他的详情页打开了吗")
    assert not _candidate_navigation_requested("他的详情页里有什么？")
    # 普通证据追问不命中。
    assert not _candidate_navigation_requested("他哪些方面匹配这个岗位")
    assert not _candidate_navigation_requested("")


class CandidateNavigationTurnTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment()))

    def _seed_assistant_tool_result(self, session_id: str, tool_calls: list[dict]) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_copilot_messages
                (session_id,context_type,context_id,role,content,structured_json)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    session_id, "global", None, "assistant", "查询结果",
                    json.dumps({"tool_calls": tool_calls}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _query_candidate_call(call_id: str, candidate_id: int, name: str, job: str) -> dict:
        return {
            "tool": "query_candidate",
            "args": {"candidate_id": candidate_id},
            "result": {
                "success": True,
                "data": {
                    "id": candidate_id, "name": name, "stage": "X1 待复核",
                    "job": job, "client": "长越科技", "stopped": False, "stop_reason": "",
                },
            },
        }

    def test_navigation_binds_candidate_from_previous_query_tool_result(self) -> None:
        self._seed_assistant_tool_result(
            "nav-tool",
            [self._query_candidate_call("call-1", 30, "张航", "机械高级工程师")],
        )

        result = self.service.copilot(
            "把他详情页拉起来",
            session_id="nav-tool",
            context={"type": "global", "id": None},
        )

        assert result["ok"] is True
        assert result["context"] == {"type": "candidate", "id": 30}
        assert result["intent_understanding"]["action"] == "open_candidate"
        assert result["turn_decision"]["effect"] == "answer"
        assert result["turn_decision"]["authorization"]["mode"] == "read_only"
        # 只读导航：不建工作流、不输出详情长文。
        assert not result["workflow_id"]
        assert "张航" in result["answer"]
        assert "已打开" in result["answer"]
        open_actions = [
            action for action in result["suggested_actions"]
            if action.get("type") == "open_candidate"
        ]
        assert open_actions, "导航回合必须发出 open_candidate 动作"
        assert int(open_actions[0]["id"]) == 30
        assert open_actions[0].get("auto") is True

    def test_query_result_writes_back_candidate_focus(self) -> None:
        self.service._persist_copilot_focus(
            "nav-focus",
            "查一下张航的情况",
            {"type": "global", "id": None},
            structured={"referenced_candidates": [{"id": 30, "name": "张航"}]},
        )
        focus = self.service.get_copilot_focus("nav-focus")
        assert isinstance(focus, dict)
        assert int((focus.get("candidate") or {}).get("id") or 0) == 30
        # context 不移动：候选人焦点只是指代绑定材料。
        assert (focus.get("context") or {}).get("type") == "global"

        result = self.service.copilot(
            "把人选的详情页打开",
            session_id="nav-focus",
            context={"type": "global", "id": None},
        )
        assert result["context"] == {"type": "candidate", "id": 30}
        assert result["intent_understanding"]["action"] == "open_candidate"
        assert any(
            action.get("type") == "open_candidate" and action.get("auto") is True
            for action in result["suggested_actions"]
        )

    def test_same_person_multiple_relations_binds_first(self) -> None:
        self._seed_assistant_tool_result(
            "nav-multi-relation",
            [
                self._query_candidate_call("call-1", 30, "张航", "机械高级工程师"),
                {
                    "tool": "query_candidate",
                    "args": {"candidate_id": 31},
                    "result": {
                        "success": True,
                        "data": {
                            "id": 31, "name": "张航", "stage": "X1 待复核",
                            "job": "另一个岗位", "client": "长越科技", "stopped": False, "stop_reason": "",
                        },
                    },
                },
            ],
        )
        result = self.service.copilot(
            "把他详情页拉起来",
            session_id="nav-multi-relation",
            context={"type": "global", "id": None},
        )
        assert result["context"] == {"type": "candidate", "id": 30}

    def test_ambiguous_people_ask_for_name(self) -> None:
        self._seed_assistant_tool_result(
            "nav-ambiguous",
            [
                self._query_candidate_call("call-1", 30, "张航", "机械高级工程师"),
                {
                    "tool": "query_candidate",
                    "args": {"candidate_id": 32},
                    "result": {
                        "success": True,
                        "data": {
                            "id": 32, "name": "李婷", "stage": "S1 待复核",
                            "job": "机械高级工程师", "client": "长越科技", "stopped": False, "stop_reason": "",
                        },
                    },
                },
            ],
        )
        result = self.service.copilot(
            "把他详情页拉起来",
            session_id="nav-ambiguous",
            context={"type": "global", "id": None},
        )
        # 多位人选：不猜，请顾问指名；不发出任何自动打开动作。
        assert result["intent_understanding"]["action"] != "open_candidate"
        assert "请带上人选姓名" in result["answer"] or "姓名" in result["answer"]
        assert not [
            action for action in result["suggested_actions"]
            if action.get("type") == "open_candidate" and action.get("auto")
        ]

    def test_navigation_without_any_candidate_gives_guidance(self) -> None:
        result = self.service.copilot(
            "把他详情页拉起来",
            session_id="nav-empty",
            context={"type": "global", "id": None},
        )
        assert result["intent_understanding"]["action"] != "open_candidate"
        assert "还没有可打开的人选" in result["answer"]

    def _seed_workflow(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)"
                " VALUES ('goal_nav1','为长越科技机械高级工程师补充候选人','寻访机械高级工程师','job',10,'{}','active')"
            )
            conn.execute(
                "INSERT INTO agent_workflows (workflow_id,goal_id,status,plan_json)"
                " VALUES ('workflow_nav1','goal_nav1','planned','{}')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_job_navigation_binds_focus_job(self) -> None:
        self.service._persist_copilot_focus("nav-job", "看看这个岗位", {"type": "job", "id": 10})
        result = self.service.copilot(
            "把这个岗位的详情页打开",
            session_id="nav-job",
            context={"type": "global", "id": None},
        )
        assert result["context"] == {"type": "job", "id": 10}
        assert result["intent_understanding"]["action"] == "open_job"
        assert result["turn_decision"]["effect"] == "answer"
        assert result["turn_decision"]["authorization"]["mode"] == "read_only"
        assert "已打开岗位详情页" in result["answer"]
        assert "机械高级工程师" in result["answer"]
        open_actions = [a for a in result["suggested_actions"] if a.get("type") == "open_job"]
        assert open_actions, "岗位导航回合必须发出 open_job 动作"
        assert int(open_actions[0]["id"]) == 10
        assert open_actions[0].get("auto") is True

    def test_job_navigation_binds_recent_query_job_result(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_copilot_messages
                (session_id,context_type,context_id,role,content,structured_json)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "nav-job-ref", "global", None, "assistant", "查询结果",
                    json.dumps({
                        "tool_calls": [{
                            "tool": "query_job",
                            "args": {"job_id": 10},
                            "result": {
                                "success": True,
                                "data": {"id": 10, "title": "机械高级工程师", "client": "长越科技"},
                            },
                        }],
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = self.service.copilot(
            "打开岗位详情页",
            session_id="nav-job-ref",
            context={"type": "global", "id": None},
        )
        assert result["context"] == {"type": "job", "id": 10}
        assert result["intent_understanding"]["action"] == "open_job"

    def test_workflow_navigation_binds_pending_workflow(self) -> None:
        self._seed_workflow()
        self.service._persist_copilot_focus(
            "nav-wf",
            "为长越科技机械高级工程师补充候选人",
            {"type": "job", "id": 10},
            structured={
                "workflow_intent": {
                    "workflow_id": "workflow_nav1", "status": "planned", "version": 1,
                    "plan_hash": "h1", "action": "candidate_sourcing",
                    "objective": "为长越科技机械高级工程师补充候选人",
                },
            },
        )
        result = self.service.copilot(
            "把这个工作流打开",
            session_id="nav-wf",
            context={"type": "global", "id": None},
        )
        assert result["context"] == {"type": "workflow", "id": "workflow_nav1"}
        assert result["intent_understanding"]["action"] == "open_workflow"
        assert result["turn_decision"]["effect"] == "answer"
        assert result["turn_decision"]["authorization"]["mode"] == "read_only"
        assert "已打开工作流" in result["answer"]
        open_actions = [a for a in result["suggested_actions"] if a.get("type") == "open_workflow"]
        assert open_actions, "工作流导航回合必须发出 open_workflow 动作"
        assert open_actions[0]["id"] == "workflow_nav1"
        assert open_actions[0].get("auto") is True

    def test_workflow_navigation_without_workflow_gives_guidance(self) -> None:
        result = self.service.copilot(
            "把这个工作流打开",
            session_id="nav-wf-empty",
            context={"type": "global", "id": None},
        )
        assert result["intent_understanding"]["action"] != "open_workflow"
        assert "还没有可打开的工作流" in result["answer"]
