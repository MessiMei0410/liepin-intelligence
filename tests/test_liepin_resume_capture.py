from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.liepin_capture import resume_matches_identity  # noqa: E402
from a_system_agent.liepin_capture import EXTRACT_RESUME_JS  # noqa: E402
from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402


def captured_resume() -> dict:
    return {
        "resume_id": "lp-resume-1",
        "source_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=lp-resume-1",
        "name": "张航",
        "status": "在职，看机会",
        "company": "ASM中国集团公司",
        "title": "高级机械设计工程师",
        "city": "上海",
        "education": "本科",
        "experience": "8年",
        "work_text": "ASM中国集团公司 高级机械设计工程师 负责精密设备机械设计",
        "project_text": "负责有限元结构分析项目",
        "education_text": "机械工程 本科",
        "full_text": "张航 ASM中国集团公司 高级机械设计工程师 8年 精密设备 有限元 本科",
        "captured_at": "2026-07-15T15:40:00",
    }


def test_masked_identity_requires_surname_company_and_title() -> None:
    resume = {
        "name": "吴酉鸣",
        "company": "实时侠智能控制技术有限公司",
        "title": "软件开发工程师",
        "full_text": "吴酉鸣 实时侠智能控制技术有限公司 软件开发工程师",
    }
    identity = {"name": "吴**", "company": "实时侠智能控制技术有限公司", "title": "软件开发工程师"}
    assert resume_matches_identity(identity, resume)
    assert not resume_matches_identity({**identity, "title": "机械设计工程师"}, resume)
    assert not resume_matches_identity({**identity, "company": "其他公司"}, resume)


def test_capture_expands_hidden_experience_and_trims_liepin_page_chrome() -> None:
    assert "显示其他\\d+段" in EXTRACT_RESUME_JS
    assert "cleanResumeLines" in EXTRACT_RESUME_JS
    assert "声明：该人选信息" in EXTRACT_RESUME_JS


class LiepinResumeCaptureServiceTest(AgentDbCase):
    def test_copilot_routes_liepin_resume_request_to_capture_skill(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        try:
            assert service._route_copilot_skills(
                "我打开猎聘简历详情页了，请补充完整简历",
                {"type": "candidate", "id": 30},
            ) == ["liepin_resume_capture"]
        finally:
            service.close()

    def test_capture_persists_source_profile_event_and_reassessment(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        with patch("a_system_agent.service.capture_open_liepin_resumes", return_value=[captured_resume()]):
            result = service.capture_liepin_resume(30)
        assert result["ok"]
        assert result["resume"]["resume_id"] == "lp-resume-1"
        assert result["assessment"]["run_id"]
        conn = sqlite3.connect(self.db_path)
        try:
            source = conn.execute(
                "SELECT source_type,source_candidate_id,raw_json FROM source_profiles WHERE person_id=20 AND source_candidate_id='lp-resume-1'"
            ).fetchone()
            assert source[0:2] == ("liepin", "lp-resume-1")
            assert "有限元结构分析" in source[2]
            profile = conn.execute(
                "SELECT profile_summary FROM candidate_profiles WHERE candidate_id=40 AND client='长越科技' AND position='机械高级工程师'"
            ).fetchone()
            assert profile is not None
            assert "有限元结构分析" in profile[0]
            event = conn.execute(
                "SELECT event_type,event_status FROM candidate_events WHERE job_candidate_id=30 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert event == ("resume_profile_captured", "completed")
        finally:
            conn.close()
            service.close()

    def test_copilot_surfaces_capture_identity_mismatch_without_model_fallback(self) -> None:
        service = AgentService(
            self.db_path,
            FakeLLM(fake_assessment(), chat_text="不应返回普通模型回答"),
        )
        wrong_resume = {
            **captured_resume(),
            "name": "李强",
            "company": "其他公司",
            "title": "其他职位",
            "full_text": "李强 其他公司 其他职位 完整工作经历和项目经历",
        }
        with patch("a_system_agent.service.capture_open_liepin_resumes", return_value=[wrong_resume]):
            result = service.copilot(
                "我打开猎聘简历详情页了，请补充完整简历",
                context={"type": "candidate", "id": 30},
            )
        try:
            assert "未能从猎聘补全当前人选的简历" in result["answer"]
            assert "已打开的猎聘简历与当前人选不匹配" in result["answer"]
            assert "不应返回普通模型回答" not in result["answer"]
            assert result["skill_runs"][0]["ok"] is False
        finally:
            service.close()
