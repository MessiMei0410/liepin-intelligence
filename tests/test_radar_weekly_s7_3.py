"""S7-3：雷达定时化 —— 信号过期降权 / 去重合并 / 周报生成器 / Copilot 提醒 / CLI 契约测试。

口径：docs/TASKCARD_S7-3_雷达定时化_20260727.md（验收 1/2/4/5）。
覆盖：
- 过期边界 59/60/61 天（is_signal_expired + 榜单降权 + 动机注入读取侧同一边界）；
- 榜单：过期信号 ×0.2 降权、不再单独成为上榜理由（全过期公司掉榜）、未过期信号不受影响；
- 去重合并：(company, type, 规范化 summary) 取最新 as_of；过期旧信号不结转、不删除；
- 周报：首期无基线如实标注；有基线出新进榜/掉出/升降对比；markdown 落盘；同日幂等；
- Copilot 提醒：payload 只含条数和入口（无公司名）；推送失败不阻断（pushed=False）；
- CLI：坏库退出码非零；注入 stub service 全链路退出码 0；管线异常退出码非零。
全部临时库 + 本地 stub，绝不打外网、不触碰真实知识库与生产 DB。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 S7-1 测试 fixture（直接单跑/模块跑均可）

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import radar_scan, radar_weekly  # noqa: E402
from asa_core.app import create_app  # noqa: E402
import radar_weekly_scan  # noqa: E402

from test_radar_scan_s7 import (  # noqa: E402
    ARTIFACTS_DDL,
    RadarApiTest,
    _StubCollector,
    _make_conn,
    _stub_extractor,
    _valid_doc,
    _valid_signal,
)

TODAY = "2026-07-27"
# 边界锚点：59 天前 2026-05-29 / 60 天前 2026-05-28 / 61 天前 2026-05-27
AGE_59 = "2026-05-29"
AGE_60 = "2026-05-28"
AGE_61 = "2026-05-27"


def _signal(company: str, as_of: str, **overrides) -> dict:
    return _valid_signal(company=company, as_of=as_of, source_urls=[f"https://example.com/{company}/{as_of}"], **overrides)


def _seed_scan(conn: sqlite3.Connection, scan_date: str, signals: list[dict], ranking: list[dict]) -> str:
    doc = _valid_doc(scan_date=scan_date, signals=signals, ranking=ranking)
    return radar_scan.upsert_radar_scan(conn, doc, radar_dir=tempfile.mkdtemp())


# ---------------------------------------------------------------------------
# 1. 过期边界契约（59/60/61 天）
# ---------------------------------------------------------------------------

class ExpiryBoundaryTest(unittest.TestCase):
    def test_59_valid_60_expired_61_expired(self) -> None:
        assert radar_scan.is_signal_expired(AGE_59, TODAY) is False, "59 天必须有效"
        assert radar_scan.is_signal_expired(AGE_60, TODAY) is True, "60 天起必须过期"
        assert radar_scan.is_signal_expired(AGE_61, TODAY) is True, "61 天必须过期"
        assert radar_scan.is_signal_expired(TODAY, TODAY) is False, "当天信号必须有效"
        assert radar_scan.is_signal_expired("2026-08-01", TODAY) is False, "未来日期不过期"

    def test_unparseable_treated_expired(self) -> None:
        assert radar_scan.is_signal_expired("", TODAY) is True
        assert radar_scan.is_signal_expired("not-a-date", TODAY) is True

    def test_load_unexpired_signals_same_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = _make_conn(Path(temp) / "m.db")
            _seed_scan(
                conn,
                TODAY,
                [_signal("芯源微", AGE_59), _signal("士兰微", AGE_60, summary="另一条信号")],
                [],
            )
            signals, _ = radar_scan.load_unexpired_signals(conn, today=TODAY)
            companies = {signal["company"] for signal in signals}
            assert companies == {"芯源微"}, f"读取侧（动机注入）同一边界：60 天信号不得注入，实际 {companies}"
            conn.close()


# ---------------------------------------------------------------------------
# 2. 榜单过期降权（验收 1：60 天信号排名下降且不再列为上榜理由；未过期不受影响）
# ---------------------------------------------------------------------------

class RankingDownweightTest(unittest.TestCase):
    def test_expired_only_company_drops_off_ranking(self) -> None:
        signals = [
            _signal("芯源微", TODAY),  # 未过期：org_change high? medium → 照常上榜
            _signal("士兰微", AGE_60),  # 仅 60 天前信号 → 掉出榜单
        ]
        ranking = radar_scan.build_ranking(signals, jobs=[], today=TODAY)
        companies = [entry["company"] for entry in ranking]
        assert companies == ["芯源微"], f"全过期公司不得上榜：{companies}"

    def test_expired_signal_downweighted_and_not_listed_as_reason(self) -> None:
        fresh = _signal("芯源微", TODAY, type="hiring", confidence="low", summary="挂牌量上升", linked_action="activate")
        stale = _signal("芯源微", AGE_61, type="risk", confidence="high", summary="公开平台显示欠薪投诉")
        ranking = radar_scan.build_ranking([fresh, stale], jobs=[], today=TODAY)
        assert len(ranking) == 1
        entry = ranking[0]
        # 未过期 2.0×0.4=0.8 + 过期 3.0×1.0×0.2=0.6 → 1.4（降权计入强度但不作理由）
        assert entry["score"] == 1.4, entry
        assert entry["signal_count"] == 1 and entry["expired_signal_count"] == 1
        assert "信号 1 条" in entry["reason"] and "招聘异动" in entry["reason"]
        assert "风险事件" not in entry["reason"], "过期信号不得列为上榜理由"
        assert "降权" in entry["reason"], "过期条数必须在理由里如实标注"
        # 建议动作取自未过期信号（hiring→activate），不被过期高风险信号带偏
        assert entry["suggested_action"] == "activate"

    def test_unexpired_unaffected(self) -> None:
        ranking = radar_scan.build_ranking([_signal("芯源微", AGE_59)], jobs=[], today=TODAY)
        assert len(ranking) == 1
        assert ranking[0]["score"] == 2.1, "59 天 org_change medium 全权重 3.0×0.7=2.1"
        assert ranking[0]["expired_signal_count"] == 0


# ---------------------------------------------------------------------------
# 3. 去重合并（新信号 ∪ 未过期旧信号，取最新 as_of；过期不删除也不结转）
# ---------------------------------------------------------------------------

class MergeSignalsTest(unittest.TestCase):
    def test_dedupe_keeps_latest_as_of(self) -> None:
        old = [_signal("芯源微", "2026-07-20", summary="公开报道显示 公司 高管变动")]
        new = [_signal("芯源微", "2026-07-25", summary="公开报道显示公司高管变动")]
        merged, stats = radar_scan.merge_with_previous_signals(old, new, today=TODAY)
        assert stats == {"carried_over": 1, "deduped": 1}
        assert len(merged) == 1 and merged[0]["as_of"] == "2026-07-25", "同键取最新 as_of"

        merged2, _ = radar_scan.merge_with_previous_signals(new, old, today=TODAY)
        assert merged2[0]["as_of"] == "2026-07-25", "旧 as_of 不覆盖新的"

    def test_expired_old_not_carried_but_distinct_kept(self) -> None:
        expired_old = _signal("士兰微", AGE_61, summary="过期旧信号")
        fresh_old = _signal("杰华特", "2026-07-10", summary="未过期旧信号")
        new = [_signal("芯源微", TODAY, summary="本期新信号")]
        merged, stats = radar_scan.merge_with_previous_signals([expired_old, fresh_old], new, today=TODAY)
        companies = {signal["company"] for signal in merged}
        assert companies == {"杰华特", "芯源微"}, f"过期旧信号不结转：{companies}"
        assert stats == {"carried_over": 1, "deduped": 0}

    def test_build_scan_merges_previous_artifact(self) -> None:
        """端到端：库里有上期榜单时，新扫描结转未过期旧信号并去重。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "芯源微_客户档案_v1.md").write_text("# 档案", encoding="utf-8")
            conn = _make_conn(root / "m.db")
            conn.executescript(
                "CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);"
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,status TEXT,target_companies TEXT);"
            )
            _seed_scan(conn, "2026-07-20", [_signal("芯源微", "2026-07-20")], [])
            collector = _StubCollector({"芯源微": [{"title": "t", "url": "https://example.com/x", "snippet": "s"}]})
            doc = radar_scan.build_radar_scan(
                conn,
                collector=collector,
                extractor=_stub_extractor,
                profiles_dir=profiles,
                kb_dir=root / "kb",
                jobs=[],
                as_of=TODAY,
            )
            # stub 本期信号 summary 与上期不同 → 两条并存（结转 1 + 新增 1）
            assert doc["stats"]["carried_over_signals"] == 1
            companies = [signal["summary"] for signal in doc["signals"]]
            assert len(companies) == 2, f"结转+新增应并存：{companies}"
            conn.close()


# ---------------------------------------------------------------------------
# 4. 周报生成器（验收 2/5：真出一份、首期无基线标注、有基线出对比、同日幂等）
# ---------------------------------------------------------------------------

class WeeklyReportTest(unittest.TestCase):
    def _fixtures(self, temp: str) -> tuple[sqlite3.Connection, Path]:
        root = Path(temp)
        conn = _make_conn(root / "m.db")
        return conn, root / "radar"

    def test_first_report_marks_no_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn, radar_dir = self._fixtures(temp)
            _seed_scan(
                conn,
                TODAY,
                [_signal("芯源微", TODAY), _signal("士兰微", AGE_61, summary="过期信号")],
                [
                    {"company": "芯源微", "score": 2.1, "reason": "信号 1 条", "suggested_action": "mapping"},
                ],
            )
            doc = radar_weekly.build_weekly_report(conn, today=TODAY)
            assert doc["schema_version"] == "radar_weekly_v1"
            assert doc["baseline"]["has_baseline"] is False
            assert doc["baseline"]["note"] == "首期，无对比基线"
            assert doc["ranking_changes"] is None
            assert doc["expired_signal_count"] == 1, "过期 1 条如实统计"
            assert len(doc["top_signals"]) == 1 and doc["top_signals"][0]["company"] == "芯源微"
            assert doc["action_summary"] == {"mapping": 1, "activate": 0, "watch": 0}
            # Copilot 提醒只含条数和入口，不含公司名（红线）
            hint = doc["copilot_hint"]["text"]
            assert "1 家新信号" in hint and "1 家建议发起 Mapping" in hint
            assert "芯源微" not in hint and "士兰微" not in hint
            markdown = radar_weekly.render_weekly_markdown(doc)
            assert "首期，无对比基线" in markdown
            assert "本周 Top 信号" in markdown and "芯源微" in markdown  # 周报正文（顾问本地）含明细
            artifact_id = radar_weekly.upsert_weekly_report(conn, doc, radar_dir=radar_dir)
            conn.commit()
            path = radar_dir / f"radar_weekly_{TODAY}.md"
            assert path.is_file(), "周报 markdown 必须落 work/radar/"
            # 同日幂等：重复生成更新同一 artifact，version 自增
            again = radar_weekly.upsert_weekly_report(conn, radar_weekly.build_weekly_report(conn, today=TODAY), radar_dir=radar_dir)
            assert again == artifact_id
            latest = radar_weekly.get_latest_weekly_report(conn)
            assert latest["weekly_report"]["version"] == 2
            assert radar_weekly.validate_weekly_report(latest["weekly_report"]) == []
            conn.close()

    def test_second_report_has_baseline_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn, radar_dir = self._fixtures(temp)
            _seed_scan(
                conn,
                "2026-07-20",
                [_signal("士兰微", "2026-07-20")],
                [
                    {"company": "士兰微", "score": 5.0, "reason": "r", "suggested_action": "mapping"},
                    {"company": "杰华特", "score": 3.0, "reason": "r", "suggested_action": "watch"},
                ],
            )
            _seed_scan(
                conn,
                TODAY,
                [_signal("芯源微", TODAY)],
                [
                    {"company": "芯源微", "score": 6.0, "reason": "r", "suggested_action": "mapping"},
                    {"company": "士兰微", "score": 2.0, "reason": "r", "suggested_action": "watch"},
                ],
            )
            doc = radar_weekly.build_weekly_report(conn, today=TODAY)
            assert doc["baseline"]["has_baseline"] is True
            assert doc["baseline"]["scan_date"] == "2026-07-20"
            changes = doc["ranking_changes"]
            assert [item["company"] for item in changes["new_entries"]] == ["芯源微"]
            assert [item["company"] for item in changes["dropped"]] == ["杰华特"]
            assert changes["risen"] == []
            assert [item["company"] for item in changes["fallen"]] == ["士兰微"]
            markdown = radar_weekly.render_weekly_markdown(doc)
            assert "首期" not in markdown
            assert "新进榜：芯源微" in markdown and "掉出榜单：杰华特" in markdown
            conn.close()

    def test_report_requires_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn, _ = self._fixtures(temp)
            with self.assertRaises(LookupError):
                radar_weekly.build_weekly_report(conn, today=TODAY)
            conn.close()


# ---------------------------------------------------------------------------
# 5. Copilot 提醒推送（payload 契约 + 失败降级不阻断）
# ---------------------------------------------------------------------------

class CopilotPushTest(unittest.TestCase):
    def test_payload_counts_only_and_failure_degrades(self) -> None:
        sent: list[dict] = []

        class _FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=0):
            sent.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse()

        hint = {"text": "本周雷达周报出了：3 家新信号，2 家建议发起 Mapping", "entry": "radar", "report_date": TODAY}
        with mock.patch.object(radar_weekly.urllib.request, "urlopen", fake_urlopen):
            result = radar_weekly.push_copilot_hint(hint, base_url="http://127.0.0.1:8765")
        assert result == {"pushed": True, "note": ""}
        payload = sent[0]
        assert payload["surface"] == "a_system" and payload["trigger"] == "radar_weekly_report"
        assert payload["explicit"] is False, "不弹窗不打扰"
        assert payload["context"]["notice"] == hint["text"]
        assert payload["context"]["page"] == "radar"
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "芯源微" not in encoded, "提醒不得含公司名等敏感细节"

        def failing_urlopen(request, timeout=0):
            raise OSError("connection refused")

        with mock.patch.object(radar_weekly.urllib.request, "urlopen", failing_urlopen):
            degraded = radar_weekly.push_copilot_hint(hint)
        assert degraded["pushed"] is False and "未送达" in degraded["note"], "推送失败必须降级留痕不外抛"


# ---------------------------------------------------------------------------
# 6. 路由契约（POST 生成 + GET latest + 404 + 幂等）
# ---------------------------------------------------------------------------

class WeeklyReportApiTest(unittest.TestCase):
    def test_post_get_latest_and_404(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "asa.db"
            RadarApiTest()._create_db(db_path)
            kb = RadarApiTest()._kb_fixture(root)
            radar_dir = root / "radar"
            app = create_app(db_path=db_path, start_legacy=False)
            collector = _StubCollector({"士兰微": [{"title": "t", "url": "https://example.com/silan", "snippet": "s"}]})
            pushed: list[dict] = []
            with (
                TestClient(app) as client,
                mock.patch.dict(os.environ, {"ASA_KNOWLEDGE_BASE_DIR": str(kb)}),
                mock.patch.object(radar_scan, "RadarCollector", lambda: collector),
                mock.patch.object(radar_scan, "llm_extract_signals", _stub_extractor),
                mock.patch.object(radar_scan, "default_radar_dir", lambda: radar_dir),
                mock.patch.object(radar_weekly, "push_copilot_hint", lambda hint, **kw: pushed.append(hint) or {"pushed": True, "note": ""}),
            ):
                missing = client.get("/api/v1/radar/weekly-report/latest")
                assert missing.status_code == 404, "尚无周报必须 404"
                no_scan = client.post(
                    "/api/v1/radar/weekly-report",
                    json={"request_id": "req-w0"},
                    headers={"Idempotency-Key": "k-w0"},
                )
                assert no_scan.status_code == 404, "尚无榜单生成周报必须 404"

                scan = client.post("/api/v1/radar/scans", json={"request_id": "req-r1"}, headers={"Idempotency-Key": "k-r1"})
                assert scan.status_code == 200, scan.text

                report = client.post(
                    "/api/v1/radar/weekly-report",
                    json={"request_id": "req-w1"},
                    headers={"Idempotency-Key": "k-w1"},
                )
                assert report.status_code == 200, report.text
                payload = report.json()
                assert payload["ok"] is True and payload["artifact_id"].startswith("radar_weekly_")
                assert payload["receipt"]["idempotent_replay"] is False
                doc = payload["weekly_report"]
                assert doc["schema_version"] == "radar_weekly_v1"
                assert doc["baseline"]["has_baseline"] is False, "首期必须标注无对比基线"
                assert Path(payload["report_file"]).is_file(), "周报 markdown 必须落盘"
                assert pushed and "新信号" in pushed[0]["text"], "生成后必须推 Copilot 提醒"
                assert payload["copilot"]["pushed"] is True

                replay = client.post(
                    "/api/v1/radar/weekly-report",
                    json={"request_id": "req-w1"},
                    headers={"Idempotency-Key": "k-w1"},
                )
                assert replay.json()["receipt"]["idempotent_replay"] is True, "同键重放首次响应"

                latest = client.get("/api/v1/radar/weekly-report/latest")
                assert latest.status_code == 200, latest.text
                detail = latest.json()
                assert detail["ok"] is True
                assert detail["artifact_id"] == payload["artifact_id"]
                assert "首期，无对比基线" in detail["content"]


# ---------------------------------------------------------------------------
# 7. CLI 契约（验收 4/5：失败退出码非零；全链路成功退出码 0）
# ---------------------------------------------------------------------------

class WeeklyScanCliTest(unittest.TestCase):
    def test_missing_db_exit_nonzero(self) -> None:
        code = radar_weekly_scan.main(["--db", "/nonexistent-dir-xyz/asa.db"])
        assert code == 2, f"数据库不存在必须非零退出，实际 {code}"

    def test_pipeline_failure_exit_nonzero(self) -> None:
        class _BoomService:
            def create_radar_scan(self, **kwargs):
                raise RuntimeError("扫描爆炸")

        code = radar_weekly_scan.main([], service_factory=lambda: _BoomService())
        assert code == 2

    def test_full_pipeline_exit_zero(self) -> None:
        calls: list[str] = []

        class _StubService:
            def create_radar_scan(self, **kwargs):
                calls.append(f"scan:{kwargs}")
                return {
                    "artifact_id": f"radar_scan_{TODAY}",
                    "radar_scan": {"stats": {"companies_scanned": 5, "signals_found": 3, "sources_failed": 0}},
                }

            def create_radar_weekly_report(self, **kwargs):
                calls.append(f"report:{kwargs}")
                return {
                    "artifact_id": f"radar_weekly_{TODAY}",
                    "report_file": "/tmp/radar_weekly.md",
                    "copilot": {"pushed": True, "note": ""},
                }

        code = radar_weekly_scan.main(
            ["--max-companies", "5", "--no-copilot-push"], service_factory=lambda: _StubService()
        )
        assert code == 0, f"全链路成功必须退出码 0，实际 {code}"
        assert calls[0] == "scan:{'max_companies': 5, 'max_workers': 1}"
        assert calls[1] == "report:{'push_copilot': False}", "--no-copilot-push 必须透传"


if __name__ == "__main__":
    unittest.main()
