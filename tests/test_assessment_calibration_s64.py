"""S6-4：评估校准学习闭环 —— 改判样例库 / 校准注入 / 采纳率度量 / 校准报告测试。

口径：docs/TASKCARD_S6-4_评估校准闭环_20260724.md（范围/红线/验收）。
全部临时库 + 临时 KB fixture + FakeLLM，绝不触碰生产 DB、真实知识库与外网 LLM。
fixture 复用 S6-1 测试模块（tests 跨文件引用是既有模式）。

覆盖（对应任务卡验收③契约测试 + 验收②度量一致性）：
1. 改判样例库：modified/rejected 自动入库（维度打标/机器原判/客户/岗位类型/as_of，无简历原文）；
   accepted/pending 移除样例；重复改判幂等更新同一行；
2. 敏感因子拒入（契约）：note 或机器原判命中年龄/性别/婚育/户籍 → 样例拒入 + blocked 日志，
   顾问动作写回本身照常 200；
3. 校准注入（契约）：同客户或同岗位类型样例注入三次 LLM payload 的 calibration 块；
   注入上限 5 条生效（7 条样例只注入 5 条）；无样例时 payload 整个不含 calibration 键；
   无关客户/岗位类型的样例不注入；
4. 注入不外泄（红线）：样例 note/机器原判不进 artifact 正文/markdown/推荐报告引用块，
   doc 只记 samples_injected 计数与 sample_ids；
5. 采纳率度量：totals 与库内 advisor_action 实际分布一致；分组 total < min_n 三个率如实 null，
   足够时非空且和为 1；
6. 校准报告：markdown 落 work/calibration/ 口径（测试用临时目录），含四个板块与样例摘录，
   空样例周如实出报；路由幂等重放返回首次响应。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import assessment_calibration, candidate_assessment  # noqa: E402
from a_system_agent.llm import FakeLLM  # noqa: E402
from asa_core.app import create_app  # noqa: E402
from test_candidate_assessment_s6 import (  # noqa: E402
    GOOD_LLM,
    DbCase,
    _fake_llm,
    _seed_person,
    _stub_fetcher,
    _valid_doc,
)


def _store_sample_doc(doc: dict, action: str, note: str) -> dict:
    """把 _valid_doc 改成指定顾问动作（供 sync_calibration_sample 直接消费）。"""
    doc.setdefault("client", "士兰微")
    doc.setdefault("job_title", "技术市场经理/总监（PC电源）")
    doc["advisor_action"] = action
    doc["advisor_note"] = note
    doc["updated_at"] = "2026-07-24 12:00:00"
    return doc


class SampleLibraryTest(DbCase):
    """改判样例库：入库 / 移除 / 幂等更新 / 敏感拒入（模块级）。"""

    def _sync(self, conn, doc: dict):
        return assessment_calibration.sync_calibration_sample(
            conn, artifact_id=f"candidate_assessment_{doc['candidate_id']}_{doc['job_id']}", doc=doc
        )

    def _rows(self, conn) -> list[sqlite3.Row]:
        return conn.execute(f"SELECT * FROM {assessment_calibration.TABLE}").fetchall()

    def test_modified_stores_sample_with_dimension_tags(self) -> None:
        conn = self.connect()
        try:
            doc = _store_sample_doc(_valid_doc(), "modified", "轨迹判断偏乐观，分位应下调一档")
            result = self._sync(conn, doc)
            assert result["stored"] is True
            assert set(result["dimensions"]) == {"trajectory", "percentile"}, "note 关键词必须打上维度标签"
            rows = self._rows(conn)
            assert len(rows) == 1
            row = rows[0]
            assert row["client"] == "士兰微"
            assert row["job_type"] == "技术市场经理/总监", "岗位类型必须归一（去括号补充说明）"
            assert row["advisor_action"] == "modified"
            assert row["as_of"] == "2026-07-24 12:00:00"
            verdicts = json.loads(row["machine_verdicts_json"])
            assert verdicts["trajectory"] == "一路上行", "必须记录机器原判"
            assert verdicts["percentile"], "必须记录分维机器原判"
            assert "简历" not in row["advisor_note"] and "张" not in row["advisor_note"]
            # 不存简历原文：样例行任何字段不得含简历片段
            whole = json.dumps(dict(row), ensure_ascii=False)
            assert "杰华特微电子股份有限公司 · 技术市场经理 负责PC电源" not in whole
        finally:
            conn.close()

    def test_untagged_note_falls_back_to_overall(self) -> None:
        conn = self.connect()
        try:
            doc = _store_sample_doc(_valid_doc(), "rejected", "整体结论与我的判断不符")
            result = self._sync(conn, doc)
            assert result["stored"] is True and result["dimensions"] == ["overall"]
            row = self._rows(conn)[0]
            verdicts = json.loads(row["machine_verdicts_json"])
            assert verdicts["overall"] == doc["consultant_summary"], "overall 机器原判取顾问口径摘要"
        finally:
            conn.close()

    def test_accepted_or_pending_removes_sample(self) -> None:
        conn = self.connect()
        try:
            self._sync(conn, _store_sample_doc(_valid_doc(), "modified", "轨迹偏乐观"))
            assert len(self._rows(conn)) == 1
            result = self._sync(conn, _store_sample_doc(_valid_doc(), "accepted", "想通了，原判没问题"))
            assert result["stored"] is False and result["reason"] == "action_not_sampled"
            assert self._rows(conn) == [], "采纳后样例必须移出校准集"
            result = self._sync(conn, _store_sample_doc(_valid_doc(), "pending", ""))
            assert result["stored"] is False
        finally:
            conn.close()

    def test_re_modified_updates_same_row(self) -> None:
        conn = self.connect()
        try:
            first = self._sync(conn, _store_sample_doc(_valid_doc(), "modified", "轨迹偏乐观"))
            second = self._sync(conn, _store_sample_doc(_valid_doc(), "modified", "动机判断也不准"))
            assert first["sample_id"] == second["sample_id"], "同一 artifact 重复改判必须更新同一行"
            rows = self._rows(conn)
            assert len(rows) == 1
            assert rows[0]["advisor_note"] == "动机判断也不准"
        finally:
            conn.close()

    def test_sensitive_note_rejected_from_library(self) -> None:
        """契约：含敏感因子（年龄/性别/婚育/户籍）的改判样例拒绝入库 + blocked 日志。"""
        conn = self.connect()
        try:
            doc = _store_sample_doc(_valid_doc(), "modified", "这个年纪偏大的人稳定性其实可以")
            result = self._sync(conn, doc)
            assert result["stored"] is False and result["reason"] == "sensitive_blocked"
            assert self._rows(conn) == [], "敏感样例必须拒入"
            log = conn.execute(
                "SELECT event_status,raw_json FROM candidate_events"
                " WHERE event_type='assessment_calibration_sample_blocked'"
            ).fetchone()
            assert log is not None and log["event_status"] == "blocked", "拒入必须记 blocked 扫描日志"
            assert "年龄" in str(log["raw_json"])
        finally:
            conn.close()

    def test_sensitive_machine_verdict_also_blocked(self) -> None:
        conn = self.connect()
        try:
            doc = _valid_doc()
            doc["dimensions"]["trajectory"]["verdict"] = "35岁正是当打之年，一路上行"
            doc = _store_sample_doc(doc, "modified", "轨迹判断口径不对")
            result = self._sync(conn, doc)
            assert result["stored"] is False and result["reason"] == "sensitive_blocked"
            assert self._rows(conn) == []
        finally:
            conn.close()


class InjectionTest(DbCase):
    """校准注入：上限生效 / 无样例无校准段 / 匹配口径 / 注入不外泄（契约）。"""

    def _capture_llm(self, captured: dict) -> FakeLLM:
        def _trajectory(payload: dict) -> dict:
            captured["trajectory"] = payload
            return GOOD_LLM

        def _pm(payload: dict) -> dict:
            captured["pm"] = payload
            return {"percentile": {"verdict": "落位前 25%"}, "motivation": {"verdict": "有变动信号"}}

        def _risks(payload: dict) -> dict:
            captured["risks"] = payload
            return {"items": []}

        return FakeLLM({}, trajectory=_trajectory, percentile_motivation=_pm, risks=_risks)

    def _seed_samples(self, count: int, *, client: str = "士兰微", job_type: str = "技术市场经理/总监") -> None:
        conn = self.connect()
        try:
            assessment_calibration.ensure_calibration_schema(conn)
            for index in range(count):
                conn.execute(
                    f"""
                    INSERT INTO {assessment_calibration.TABLE}
                    (artifact_id,candidate_id,job_id,client,job_type,advisor_action,
                     dimensions_json,machine_verdicts_json,advisor_note,as_of)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"candidate_assessment_x{index}_{client}",
                        9000 + index,
                        154,
                        client,
                        job_type,
                        "modified",
                        json.dumps(["percentile"], ensure_ascii=False),
                        json.dumps({"percentile": f"机器原判{index}：落位前 10%"}, ensure_ascii=False),
                        f"改判口径{index}：这个客户不看分位，接受平移",
                        f"2026-07-2{index % 10} 10:00:00",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _run(self, captured: dict) -> dict:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        conn = self.connect()
        try:
            return candidate_assessment.run_assessment(
                conn, candidate_id=1, job_id=154, llm=self._capture_llm(captured),
                kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher,
            )
        finally:
            conn.close()

    def test_no_samples_no_calibration_block(self) -> None:
        """契约：无样例时三次 LLM payload 整个不含 calibration 键（不编造、不空注入）。"""
        captured: dict = {}
        doc = self._run(captured)
        for name in ("trajectory", "pm", "risks"):
            assert "calibration" not in captured[name], f"{name} payload 无样例时不得含校准段"
        assert doc["calibration_stats"]["samples_injected"] == 0
        assert doc["calibration_stats"]["sample_ids"] == []

    def test_injection_limit_five(self) -> None:
        """契约：7 条同客户样例只注入最近 5 条（上限生效，id 倒序取新）。"""
        self._seed_samples(7)
        captured: dict = {}
        doc = self._run(captured)
        for name in ("trajectory", "pm", "risks"):
            block = captured[name].get("calibration")
            assert block is not None, f"{name} payload 必须注入校准段"
            assert len(block["examples"]) == assessment_calibration.INJECT_LIMIT == 5
            assert "不得因此放宽证据要求" in block["instruction"], "证据强约束必须写进注入 instruction"
            assert "严禁" in block["instruction"], "永不外泄约束必须写进注入 instruction"
        notes = [ex["advisor_correction"] for ex in captured["trajectory"]["calibration"]["examples"]]
        assert notes[0] == "改判口径6：这个客户不看分位，接受平移", "必须取最近样例（id 倒序）"
        assert "改判口径0" not in " ".join(notes) and "改判口径1" not in " ".join(notes)
        assert doc["calibration_stats"]["samples_injected"] == 5
        assert len(doc["calibration_stats"]["sample_ids"]) == 5

    def test_unrelated_client_and_job_type_not_injected(self) -> None:
        self._seed_samples(3, client="鹏新旭", job_type="模拟设计工程师")
        captured: dict = {}
        self._run(captured)
        assert "calibration" not in captured["trajectory"], "无关客户/岗位类型的样例不得注入"

    def test_injection_never_leaks_to_artifact_or_report(self) -> None:
        """红线：样例 note/机器原判不进 artifact 正文、markdown、推荐报告引用块。"""
        self._seed_samples(2)
        captured: dict = {}
        doc = self._run(captured)
        assert captured["trajectory"]["calibration"]["examples"], "注入必须发生（前置）"
        blob = json.dumps(doc, ensure_ascii=False)
        assert "改判口径" not in blob, "样例 note 不得落 artifact doc"
        assert "机器原判" not in blob, "机器原判文本不得落 artifact doc"
        markdown = candidate_assessment._artifact_markdown(doc)
        assert "改判口径" not in markdown and "机器原判" not in markdown
        block = candidate_assessment.report_reference_block(doc)
        report_text = json.dumps(block, ensure_ascii=False)
        assert "改判口径" not in report_text and "机器原判" not in report_text
        # 校验闸不受影响：calibration_stats 只是计数，schema 校验照常通过
        assert candidate_assessment.validate_assessment(doc) == []


class MetricsTest(DbCase):
    """采纳率度量（验收②）：totals 与库内 advisor_action 分布一致；不足 null。"""

    def _persist(self, conn, candidate_id: int, job_id: int, action: str, note: str = "", client: str = "") -> None:
        doc = _valid_doc(candidate_id=candidate_id, job_id=job_id)
        doc["client"] = client or "士兰微"
        doc["job_title"] = "技术市场经理/总监（PC电源）"
        candidate_assessment.upsert_assessment(conn, doc)
        if action != "pending":
            doc["advisor_action"] = action
            doc["advisor_note"] = note
            doc["updated_at"] = "2026-07-24 13:00:00"
            candidate_assessment.upsert_assessment(conn, doc)
            assessment_calibration.sync_calibration_sample(
                conn, artifact_id=f"candidate_assessment_{candidate_id}_{job_id}", doc=doc
            )
        conn.commit()

    def test_totals_match_advisor_action_distribution(self) -> None:
        conn = self.connect()
        try:
            self._persist(conn, 1, 154, "accepted")
            self._persist(conn, 2, 154, "modified", note="轨迹偏乐观")
            self._persist(conn, 3, 154, "rejected", note="整体不对")
            self._persist(conn, 4, 154, "pending")
            metrics = assessment_calibration.compute_metrics(conn)
            totals = metrics["totals"]
            # 与库内实际分布逐字段一致（验收②）
            dist = {"pending": 0, "accepted": 0, "modified": 0, "rejected": 0}
            for row in conn.execute(
                "SELECT metadata_json FROM agent_artifacts WHERE artifact_type='candidate_assessment'"
            ).fetchall():
                dist[json.loads(row["metadata_json"]).get("advisor_action", "pending")] += 1
            assert totals["assessments"] == 4
            for action, count in dist.items():
                assert totals[action] == count, f"{action} 计数必须与库内分布一致"
        finally:
            conn.close()

    def test_insufficient_groups_return_null_rates(self) -> None:
        conn = self.connect()
        try:
            self._persist(conn, 1, 154, "accepted")  # 士兰微五维各 1 采纳 → total=1 < 3 → null
            self._persist(conn, 2, 154, "modified", note="轨迹偏乐观")  # trajectory total=2 → null
            metrics = assessment_calibration.compute_metrics(conn)
            assert metrics["min_n"] == 3
            by_dim = {g["dimension"]: g for g in metrics["groups"] if g["client"] == "士兰微"}
            trajectory = by_dim["trajectory"]
            assert trajectory["total"] == 2 and trajectory["accepted"] == 1 and trajectory["modified"] == 1
            assert trajectory["acceptance_rate"] is None, "数据不足必须如实 null"
            assert trajectory["modified_rate"] is None and trajectory["rejected_rate"] is None
            assert metrics["labels"]["acceptance_rate"] == "顾问点头率", "文案必须业务语言"
        finally:
            conn.close()

    def test_sufficient_group_rates_sum_to_one(self) -> None:
        conn = self.connect()
        try:
            for candidate_id in (1, 2, 3):
                self._persist(conn, candidate_id, 154, "accepted")
            self._persist(conn, 4, 154, "modified", note="轨迹偏乐观")
            metrics = assessment_calibration.compute_metrics(conn)
            by_dim = {g["dimension"]: g for g in metrics["groups"] if g["client"] == "士兰微"}
            trajectory = by_dim["trajectory"]
            assert trajectory["total"] == 4
            assert trajectory["acceptance_rate"] == 0.75
            assert trajectory["modified_rate"] == 0.25 and trajectory["rejected_rate"] == 0.0
            motivation = by_dim["motivation"]
            assert motivation["acceptance_rate"] == 1.0, "采纳 = 全维点头"
        finally:
            conn.close()


class ReportTest(DbCase):
    """校准报告：markdown 板块齐全 / 空周如实 / 输出只在指定目录。"""

    def test_report_markdown_structure(self) -> None:
        conn = self.connect()
        try:
            assessment_calibration.ensure_calibration_schema(conn)
            for index, note in enumerate(("轨迹偏乐观", "轨迹判断口径不对", "整体结论不符")):
                doc = _store_sample_doc(_valid_doc(candidate_id=10 + index), "modified", note)
                doc["artifact"] = None
                assessment_calibration.sync_calibration_sample(
                    conn, artifact_id=f"candidate_assessment_{10 + index}_154", doc=doc
                )
            conn.commit()
            out_dir = Path(self.db_temp.name) / "work" / "calibration"
            result = assessment_calibration.generate_report(conn, out_dir=out_dir)
            assert result["ok"] is True
            path = Path(result["path"])
            assert path.is_file() and str(out_dir) in str(path), "报告只写指定输出目录"
            text = path.read_text(encoding="utf-8")
            for token in ("评估校准周报", "本周改判概览", "改判集中的维度", "客户口径观察", "系统性偏差建议", "改判样例摘录"):
                assert token in text, f"报告必须含「{token}」板块"
            assert "轨迹偏乐观" in text, "内部留档报告可含改判 note"
            assert "职业轨迹" in text, "维度必须业务语言"
            assert result["stats"]["samples"] == 3
        finally:
            conn.close()

    def test_empty_week_report_is_honest(self) -> None:
        conn = self.connect()
        try:
            out_dir = Path(self.db_temp.name) / "work" / "calibration"
            result = assessment_calibration.generate_report(conn, out_dir=out_dir)
            text = Path(result["path"]).read_text(encoding="utf-8")
            assert "本周无改判样例" in text
            assert result["stats"]["samples"] == 0
        finally:
            conn.close()


class CalibrationApiTest(DbCase):
    """路由：PATCH 动作携校准回执 / GET metrics / POST report（幂等）。"""

    def _prepare(self) -> TestClient:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        app = create_app(db_path=self.db_path, start_legacy=False)
        app.state.core.agent_service.llm = _fake_llm()
        app.state.core.agent_service.assessment_signal_fetcher = _stub_fetcher
        return TestClient(app)

    @staticmethod
    def _generate(client: TestClient) -> None:
        # 必须在 with 块内调用：lifespan 跑 migrate 建 api_idempotency 等治理表
        response = client.post(
            "/api/v1/candidates/1/assessments?job_id=154",
            json={"request_id": "req-gen"}, headers={"Idempotency-Key": "k-gen"},
        )
        assert response.status_code == 200, response.text

    def test_patch_modified_returns_calibration_receipt(self) -> None:
        with self._prepare() as client:
            self._generate(client)
            response = client.patch(
                "/api/v1/candidates/1/assessments/154/advisor-action",
                json={"request_id": "req-m1", "action": "modified", "note": "轨迹判断偏乐观，分位应下调"},
                headers={"Idempotency-Key": "k-m1"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["calibration"]["stored"] is True
            assert set(payload["calibration"]["dimensions"]) == {"trajectory", "percentile"}
            conn = self.connect()
            try:
                rows = conn.execute(f"SELECT * FROM {assessment_calibration.TABLE}").fetchall()
                assert len(rows) == 1
            finally:
                conn.close()

    def test_patch_sensitive_note_200_but_sample_blocked(self) -> None:
        """契约：敏感改判 note → 动作写回照常 200，样例拒入且回执说明原因。"""
        with self._prepare() as client:
            self._generate(client)
            response = client.patch(
                "/api/v1/candidates/1/assessments/154/advisor-action",
                json={"request_id": "req-m2", "action": "modified", "note": "已婚已育的人选稳定性其实没问题"},
                headers={"Idempotency-Key": "k-m2"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["advisor_action"] == "modified", "敏感只拦样例，不拦动作写回"
            assert payload["calibration"]["stored"] is False
            assert payload["calibration"]["reason"] == "sensitive_blocked"
            conn = self.connect()
            try:
                rows = conn.execute(f"SELECT * FROM {assessment_calibration.TABLE}").fetchall()
                assert rows == []
            finally:
                conn.close()

    def test_metrics_endpoint(self) -> None:
        with self._prepare() as client:
            self._generate(client)
            client.patch(
                "/api/v1/candidates/1/assessments/154/advisor-action",
                json={"request_id": "req-m3", "action": "modified", "note": "轨迹偏乐观"},
                headers={"Idempotency-Key": "k-m3"},
            )
            response = client.get("/api/v1/assessments/calibration/metrics")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is True
            assert payload["totals"]["modified"] == 1 and payload["totals"]["assessments"] == 1
            group = next(g for g in payload["groups"] if g["dimension"] == "trajectory")
            assert group["client"] == "士兰微" and group["acceptance_rate"] is None
            assert payload["labels"]["title"] == "评估校准 · 顾问点头率"

    def test_report_endpoint_idempotent(self) -> None:
        with self._prepare() as client:
            self._generate(client)
            first = client.post(
                "/api/v1/assessments/calibration/report",
                json={"request_id": "req-r1"}, headers={"Idempotency-Key": "k-r1"},
            )
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["ok"] is True
            path = Path(payload["path"])
            assert path.is_file()
            assert "work/calibration" in str(path).replace("\\", "/"), "报告必须落 work/calibration/"
            assert payload["receipt"]["idempotent_replay"] is False
            replay = client.post(
                "/api/v1/assessments/calibration/report",
                json={"request_id": "req-r1"}, headers={"Idempotency-Key": "k-r1"},
            )
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True
            path.unlink(missing_ok=True)  # 测试落仓内 work/calibration/ 的报告，用完即清


if __name__ == "__main__":
    unittest.main()
