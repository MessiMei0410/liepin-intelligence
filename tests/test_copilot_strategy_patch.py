"""Copilot 策略建议一键同步（strategy_patch）回归守护。

覆盖：
1. 门控 `_strategy_patch_candidate`——只有含策略要素的建议类回答才触发提取；
   修订请求消息（走 _strategy_revision_requested 通道）不重复出 patch。
2. `_strategy_v2_existing_values`——从 strategy_v2 提取现有词条（关键词组 terms、
   公司池、排除规则）用于服务端去重。
3. `_normalize_strategy_patch_changes`——类型/长度校验、patch 内去重、对现有策略
   去重、置信度降序、20 项封顶、clause 文案。
4. `_build_strategy_patch` 端到端（stub service + 临时 DB）——出 patch、LLM 失败
   返回 None、全重复返回 None、寻访已开始（不可修订）返回 None。
5. 浮窗契约——操作栏/Diff 弹层/应用逻辑的源码标记；node 执行真实交付 JS 验证
   renderStrategyPatchBar 三态渲染。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
SERVER_SOURCE = ROOT / "scripts" / "liepin_workbench_server.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent.copilot_handler import (  # noqa: E402
    _build_strategy_patch,
    _copilot_context_from_focus,
    _copilot_workflow_context_facts,
    _normalize_strategy_patch_changes,
    _resolve_strategy_revision_workflow,
    _strategy_patch_candidate,
    _strategy_v2_existing_values,
)


class _StubLLM:
    def __init__(self, patch):
        self._patch = patch
        self.calls = 0

    def extract_strategy_patch(self, payload):
        self.calls += 1
        return self._patch


class _StubService:
    def __init__(self, db_path: Path, llm) -> None:
        self._db_path = db_path
        self.llm = llm

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


_PATCH_SCHEMA = """
CREATE TABLE agent_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT, title TEXT, objective TEXT,
    context_type TEXT, context_id INTEGER, context_json TEXT DEFAULT '{}'
);
CREATE TABLE agent_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT, goal_id TEXT, status TEXT, created_at TEXT
);
CREATE TABLE agent_workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT, capability_id TEXT, status TEXT, sequence INTEGER,
    output_json TEXT DEFAULT '{}'
);
"""

_STRATEGY_V2 = {
    "step2_target_pool": [
        {"path": "T1 竞对", "companies": [{"name": "日立"}, {"name": "台达"}]},
    ],
    "step4_keyword_groups": [
        {"group": "g1", "targets": ["日立"], "terms": ["服务器电源", "电源模块"]},
    ],
    "negative_rules": [{"type": "industry", "rule": "排除非半导体背景"}],
}

_ANSWER = "建议补充关键词「通信电源」「基站电源」，并扩展对标公司台达、维谛；也可以加过滤条件排除销售岗。"


def _make_db(tmp: Path, *, sourcing_status: str = "pending", workflow_status: str = "planned") -> Path:
    db_path = tmp / "agent.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_PATCH_SCHEMA)
    conn.execute(
        "INSERT INTO agent_goals(goal_id,title,objective,context_type,context_id,context_json) VALUES ('goal_1','士兰微电源专家寻访','obj','job',10,'{}')"
    )
    conn.execute(
        "INSERT INTO agent_workflows(workflow_id,goal_id,status,created_at) VALUES ('workflow_abc123','goal_1',?,'2026-07-29')",
        (workflow_status,),
    )
    conn.execute(
        "INSERT INTO agent_workflow_steps(workflow_id,capability_id,status,sequence,output_json) VALUES ('workflow_abc123','search_strategy','completed',1,?)",
        (json.dumps({"strategy_v2": _STRATEGY_V2}, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO agent_workflow_steps(workflow_id,capability_id,status,sequence) VALUES ('workflow_abc123','multi_channel_sourcing',?,2)",
        (sourcing_status,),
    )
    conn.commit()
    conn.close()
    return db_path


class GateTest(unittest.TestCase):
    def test_strategy_suggestion_answer_is_candidate(self) -> None:
        assert _strategy_patch_candidate("策略上还有什么建议", _ANSWER) is True

    def test_non_strategy_answer_skipped(self) -> None:
        assert _strategy_patch_candidate("他简历怎么样", "结论：这个人选不错，建议尽快约面。") is False

    def test_revision_request_uses_the_same_confirmation_patch(self) -> None:
        assert _strategy_patch_candidate("修改一下寻访策略", _ANSWER) is True

    def test_empty_answer_skipped(self) -> None:
        assert _strategy_patch_candidate("随便", "") is False


class ExistingValuesTest(unittest.TestCase):
    def test_extracts_terms_companies_rules(self) -> None:
        existing = _strategy_v2_existing_values(_STRATEGY_V2)
        assert "服务器电源" in existing["terms"]
        assert "电源模块" in existing["terms"]
        assert "日立" in existing["companies"]
        assert "台达" in existing["companies"]
        assert "排除非半导体背景" in existing["rules"]

    def test_empty_v2(self) -> None:
        existing = _strategy_v2_existing_values({})
        assert existing == {"terms": set(), "companies": set(), "rules": set()}


class NormalizeTest(unittest.TestCase):
    def _existing(self) -> dict:
        return _strategy_v2_existing_values(_STRATEGY_V2)

    def test_validates_types_and_lengths(self) -> None:
        changes = _normalize_strategy_patch_changes(
            [
                {"type": "add_keyword", "value": "通信电源"},
                {"type": "delete_keyword", "value": "非法类型"},
                {"type": "add_keyword", "value": "长"},
                {"type": "add_company", "value": "x" * 41},
                {"type": "add_scene", "value": ""},
                "junk",
            ],
            self._existing(),
        )
        assert [c["value"] for c in changes] == ["通信电源"]
        assert changes[0]["field"] == "keywords"
        assert changes[0]["clause"] == "新增关键词「通信电源」"

    def test_dedupes_within_patch_and_against_existing(self) -> None:
        changes = _normalize_strategy_patch_changes(
            [
                {"type": "add_keyword", "value": "通信电源"},
                {"type": "add_keyword", "value": " 通信 电源 "},
                {"type": "add_keyword", "value": "服务器电源"},  # 已存在 terms
                {"type": "add_company", "value": "台达"},          # 已存在公司池
                {"type": "add_company", "value": "维谛"},
                {"type": "add_filter", "value": "排除非半导体背景"},  # 已存在规则
                {"type": "add_filter", "value": "排除销售岗"},
            ],
            self._existing(),
        )
        assert [(c["type"], c["value"]) for c in changes] == [
            ("add_keyword", "通信电源"),
            ("add_company", "维谛"),
            ("add_filter", "排除销售岗"),
        ]

    def test_sorts_by_confidence_and_caps_at_20(self) -> None:
        raw = [{"type": "add_keyword", "value": f"关键词{i:02d}", "confidence": i / 100} for i in range(30)]
        changes = _normalize_strategy_patch_changes(raw, self._existing())
        assert len(changes) == 20
        confidences = [c["confidence"] for c in changes]
        assert confidences == sorted(confidences, reverse=True)

    def test_bad_confidence_falls_back(self) -> None:
        changes = _normalize_strategy_patch_changes(
            [{"type": "add_keyword", "value": "通信电源", "confidence": "很高"}],
            self._existing(),
        )
        assert changes[0]["confidence"] == 0.5


class BuildPatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def test_builds_patch_with_dedupe_and_instruction_parts(self) -> None:
        db_path = _make_db(self.tmp)
        llm = _StubLLM({"changes": [
            {"type": "add_keyword", "value": "通信电源", "confidence": 0.9},
            {"type": "add_keyword", "value": "服务器电源", "confidence": 0.8},  # 已存在，应被去重
            {"type": "add_company", "value": "维谛", "confidence": 0.7},
        ]})
        service = _StubService(db_path, llm)
        patch = _build_strategy_patch(service, "策略上还有什么建议", _ANSWER, {"type": "job", "id": 10})
        assert patch is not None
        assert patch["version"] == "1.0"
        assert patch["source"] == "copilot"
        assert patch["workflow_id"] == "workflow_abc123"
        assert patch["workflow_title"] == "士兰微电源专家寻访"
        assert [c["value"] for c in patch["changes"]] == ["通信电源", "维谛"]
        assert patch["instruction_prefix"].startswith("仅修订当前工作流的寻访策略")
        assert "逐项读取原 strategy_v2" in patch["instruction_suffix"]
        assert llm.calls == 1

    def test_llm_failure_returns_none(self) -> None:
        db_path = _make_db(self.tmp)
        service = _StubService(db_path, _StubLLM(None))
        patch = _build_strategy_patch(service, "策略上还有什么建议", _ANSWER, {"type": "job", "id": 10})
        assert patch is None

    def test_all_duplicated_returns_none(self) -> None:
        db_path = _make_db(self.tmp)
        llm = _StubLLM({"changes": [{"type": "add_keyword", "value": "服务器电源"}]})
        service = _StubService(db_path, llm)
        patch = _build_strategy_patch(service, "策略上还有什么建议", _ANSWER, {"type": "job", "id": 10})
        assert patch is None

    def test_sourcing_started_returns_none(self) -> None:
        db_path = _make_db(self.tmp, sourcing_status="completed", workflow_status="completed")
        llm = _StubLLM({"changes": [{"type": "add_keyword", "value": "通信电源"}]})
        service = _StubService(db_path, llm)
        patch = _build_strategy_patch(service, "策略上还有什么建议", _ANSWER, {"type": "job", "id": 10})
        assert patch is None
        assert llm.calls == 0  # 不可修订时连 LLM 提取都不触发

    def test_selected_workflow_context_resolves_exact_target_when_job_has_multiple_rounds(self) -> None:
        db_path = _make_db(self.tmp)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO agent_goals(goal_id,title,objective,context_type,context_id,context_json) VALUES ('goal_2','士兰微电源专家第2轮寻访','obj','job',10,'{}')"
        )
        conn.execute(
            "INSERT INTO agent_workflows(workflow_id,goal_id,status,created_at) VALUES ('workflow_def456','goal_2','planned','2026-07-30')"
        )
        conn.execute(
            "INSERT INTO agent_workflow_steps(workflow_id,capability_id,status,sequence) VALUES ('workflow_def456','search_strategy','completed',1)"
        )
        conn.execute(
            "INSERT INTO agent_workflow_steps(workflow_id,capability_id,status,sequence) VALUES ('workflow_def456','multi_channel_sourcing','pending',2)"
        )
        conn.commit()
        conn.close()
        service = _StubService(db_path, _StubLLM(None))

        workflow_id, error = _resolve_strategy_revision_workflow(
            service, "把关键词补充为通信电源", {"type": "workflow", "id": "workflow_def456"}
        )

        assert error == ""
        assert workflow_id == "workflow_def456"

    def test_workflow_context_has_its_own_facts_and_wins_over_saved_focus(self) -> None:
        db_path = _make_db(self.tmp)

        class FocusService(_StubService):
            _copilot_workflow_context_facts = _copilot_workflow_context_facts

            def _copilot_focus_context_facts(self, _context: dict):
                return {}

            def get_copilot_focus(self, _session_id: str):
                return {
                    "context": {"type": "job", "id": 999},
                    "client": "长越科技",
                    "job": {"id": 999, "title": "自动化软件高级工程师"},
                    "confidence": 1.0,
                }

            def _mentioned_client_names(self, _message: str):
                return []

            def _mentioned_jobs_for_copilot(self, _message: str):
                return []

            def _copilot_context_facts(self, context: dict):
                return self._copilot_workflow_context_facts(context)

        service = FocusService(db_path, _StubLLM(None))
        facts = _copilot_workflow_context_facts(service, {"type": "workflow", "id": "workflow_abc123"})
        selected, conflicts = _copilot_context_from_focus(
            service, "session-1", "策略做下优化，更多关键词和目标公司", {"type": "workflow", "id": "workflow_abc123"}
        )

        assert facts["context"] == {"type": "workflow", "id": "workflow_abc123"}
        assert facts["workflow"]["title"] == "士兰微电源专家寻访"
        assert selected == {"type": "workflow", "id": "workflow_abc123"}
        assert conflicts == []

    def test_gate_failure_skips_llm(self) -> None:
        db_path = _make_db(self.tmp)
        llm = _StubLLM({"changes": [{"type": "add_keyword", "value": "通信电源"}]})
        service = _StubService(db_path, llm)
        patch = _build_strategy_patch(service, "他怎么样", "结论：这个人选不错。", {"type": "job", "id": 10})
        assert patch is None
        assert llm.calls == 0


# ---------------------------------------------------------------------------
# 浮窗契约：源码标记
# ---------------------------------------------------------------------------


def _server_text() -> str:
    return SERVER_SOURCE.read_text(encoding="utf-8")


def test_floating_strategy_patch_rendering_contract() -> None:
    source = _server_text()
    for marker in [
        "// --- asa-floating-strategy-patch ---",
        "// --- end asa-floating-strategy-patch ---",
        "renderStrategyPatchBar",
        "openStrategyPatchModal",
        "closeStrategyPatchModal",
        "copyStrategyPatch",
        "applyStrategyPatch",
        "STRATEGY_PATCH_TYPE_LABELS",
        "data-patch-apply",
        "data-patch-copy",
        "data-patch-ignore",
        "data-patch-change",
        "data-patch-confirm",
        "data-patch-revert",
        "strategy_patch_applied",
        "strategy_patch_ignored",
        "revert_revision",
        "trackCopilotEvent",
        "strategy_patch_revert_expired",
        "应用到策略",
        "策略变更预览",
        "确认应用",
        "result.strategy_patch",
        "patch.instruction_prefix",
        "patch.instruction_suffix",
        "consultant_evidence",
        "strategy_patch_restored_workflow_id",
        "/revise",
        "Idempotency-Key",
        "openWorkbenchUrl(`/asa-app#workflow=${encodeURIComponent(message.strategy_patch_revised_workflow_id)}`)",
        "restoredWorkflowId",
    ]:
        assert marker in source, marker


def test_floating_strategy_patch_css_contract() -> None:
    source = _server_text()
    for marker in [
        ".patch-modal-backdrop",
        ".patch-modal",
        ".patch-group",
        ".patch-item",
        ".strategy-patch-done",
    ]:
        assert marker in source, marker


def test_session_restore_passes_strategy_patch() -> None:
    handler_source = (SCRIPTS_DIR / "a_system_agent" / "copilot_handler.py").read_text(encoding="utf-8")
    assert '"strategy_patch": structured.get("strategy_patch")' in handler_source
    assert '"strategy_patch": strategy_patch' in handler_source
    assert 'assistant_structured["strategy_patch"] = strategy_patch' in handler_source


# ---------------------------------------------------------------------------
# 浮窗行为：node 执行真实交付 JS（renderStrategyPatchBar 三态）
# ---------------------------------------------------------------------------

_NODE_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const section = src.match(/\/\/ --- asa-floating-strategy-patch ---([\s\S]*?)\/\/ --- end asa-floating-strategy-patch ---/)[1];
const escFn = src.match(/function esc\(v\)\{[^\n]*\}/)[0];
const factory = new Function(`${escFn}\n${section}\nreturn { renderStrategyPatchBar, STRATEGY_PATCH_TYPE_LABELS };`);
const t = factory();
const patch = {
  workflow_id: 'workflow_abc123',
  workflow_title: '士兰微电源专家寻访',
  changes: [{ type: 'add_keyword', value: '通信电源', clause: '新增关键词「通信电源」' }],
};
const out = {
  normal: t.renderStrategyPatchBar({ strategy_patch: patch }, 3),
  ignored: t.renderStrategyPatchBar({ strategy_patch: patch, strategy_patch_ignored: true }, 3),
  applied: t.renderStrategyPatchBar({ strategy_patch: patch, strategy_patch_applied: true }, 3),
  noPatch: t.renderStrategyPatchBar({ strategy_patch: null }, 0),
  emptyChanges: t.renderStrategyPatchBar({ strategy_patch: { changes: [] } }, 0),
  labels: t.STRATEGY_PATCH_TYPE_LABELS,
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def patch_bar_behavior() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过浮窗策略补丁 JS 行为测试")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(SERVER_SOURCE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_patch_bar_three_states(patch_bar_behavior: dict) -> None:
    normal = patch_bar_behavior["normal"]
    assert 'data-patch-apply="3"' in normal
    assert 'data-patch-copy="3"' in normal
    assert 'data-patch-ignore="3"' in normal
    assert "应用到策略" in normal
    assert patch_bar_behavior["ignored"] == ""
    assert "已应用到策略" in patch_bar_behavior["applied"]
    assert patch_bar_behavior["noPatch"] == ""
    assert patch_bar_behavior["emptyChanges"] == ""


def test_patch_type_labels(patch_bar_behavior: dict) -> None:
    labels = patch_bar_behavior["labels"]
    assert labels == {"add_keyword": "关键词", "add_company": "对标公司", "add_scene": "场景词", "add_filter": "过滤条件"}


if __name__ == "__main__":
    unittest.main()
