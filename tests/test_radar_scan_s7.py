"""S7-1：人才流动雷达 —— radar_scan 数据模型 / 公司池 / 采集器边界 / 榜单 / 路由测试。

口径：docs/TASKCARD_S7-1_人才流动雷达_20260724.md（验收标准）+ PRD S7 §1/§3/§4。
全部使用临时库 + 临时 KB/档案 fixture（运行时只读，绝不触碰真实知识库与生产 DB）；
采集器与 LLM 抽取一律注入本地 stub，绝不打外网。
覆盖：schema 必备键/版本校验、无 source_urls 信号写入被拒（硬锚点）、六类枚举校验、
LLM 编造 URL 剥离、同日幂等 upsert、禁挖公司信号过滤、榜单打分可解释、
路由 POST/GET 200/404/幂等、restricted 不回泄。
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

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import radar_scan  # noqa: E402
from asa_core.app import create_app  # noqa: E402

ARTIFACTS_DDL = """
CREATE TABLE agent_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    step_id INTEGER,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'text/markdown',
    file_path TEXT,
    content TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

FORBIDDEN_LITERALS = ["13912345678", "费率23%", "MARKER_REDLINE_S7", "话术红线"]


def _valid_signal(**overrides) -> dict:
    signal = {
        "company": "芯源微",
        "type": "org_change",
        "summary": "公开报道显示公司高管变动",
        "implication": "骨干观望期，可关注",
        "source_urls": ["https://example.com/news/1"],
        "as_of": "2026-07-24",
        "confidence": "medium",
        "linked_action": "mapping",
    }
    signal.update(overrides)
    return signal


def _valid_doc(**overrides) -> dict:
    doc = {
        "schema_version": "radar_v1",
        "scan_date": "2026-07-24",
        "generated_at": "2026-07-24 10:00:00",
        "company_pool": [{"company": "芯源微", "origin": "client_profile"}],
        "signals": [_valid_signal()],
        "ranking": [
            {"company": "芯源微", "score": 3.0, "reason": "信号 1 条（组织/高管变动×1）", "suggested_action": "mapping"}
        ],
        "stats": {
            "companies_scanned": 1,
            "companies_with_signals": 1,
            "signals_found": 1,
            "sources_failed": 0,
            "banned_filtered": 0,
            "rejected_no_source": 0,
            "rejected_invalid": 0,
        },
    }
    doc.update(overrides)
    return doc


def _make_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(ARTIFACTS_DDL)
    return conn


# ---------------------------------------------------------------------------
# 1. schema 校验（必备键 / 版本 / 日期 / ranking / stats）
# ---------------------------------------------------------------------------

class SchemaValidationTest(unittest.TestCase):
    def test_valid_doc_passes(self) -> None:
        assert radar_scan.validate_radar_scan(_valid_doc()) == []

    def test_missing_required_keys_and_bad_version(self) -> None:
        assert radar_scan.validate_radar_scan({"schema_version": "radar_v1"}) != []
        doc = _valid_doc(schema_version="radar_v0")
        assert any("schema_version" in error for error in radar_scan.validate_radar_scan(doc))
        doc = _valid_doc(scan_date="2026/07/24")
        assert any("scan_date" in error for error in radar_scan.validate_radar_scan(doc))
        doc = _valid_doc(ranking=[{"company": "芯源微", "score": "高", "reason": ""}])
        errors = radar_scan.validate_radar_scan(doc)
        assert any("score" in error for error in errors) and any("reason" in error for error in errors)
        doc = _valid_doc(stats={"companies_scanned": "1", "signals_found": 1, "sources_failed": 0})
        assert any("companies_scanned" in error for error in radar_scan.validate_radar_scan(doc))

    def test_summary_required_implication_optional(self) -> None:
        doc = _valid_doc(signals=[_valid_signal(summary="  ")])
        assert any("summary" in error for error in radar_scan.validate_radar_scan(doc))
        doc = _valid_doc(signals=[_valid_signal(implication="")])
        assert radar_scan.validate_radar_scan(doc) == [], "implication 允许为空（只记事实）"


# ---------------------------------------------------------------------------
# 2. 无 source_urls 信号写入被拒（硬锚点）+ 六类枚举校验
# ---------------------------------------------------------------------------

class NoSourceRejectedTest(unittest.TestCase):
    def test_validate_rejects_empty_source_urls(self) -> None:
        doc = _valid_doc(signals=[_valid_signal(source_urls=[])])
        assert any("source_urls" in error for error in radar_scan.validate_radar_scan(doc))
        doc = _valid_doc(signals=[_valid_signal(source_urls=["  ", ""])])
        assert any("source_urls" in error for error in radar_scan.validate_radar_scan(doc))

    def test_upsert_refuses_whole_doc_when_signal_has_no_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = _make_conn(Path(temp) / "m.db")
            doc = _valid_doc(signals=[_valid_signal(source_urls=[])])
            with self.assertRaises(ValueError):
                radar_scan.upsert_radar_scan(conn, doc, radar_dir=Path(temp) / "radar")
            count = conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]
            assert count == 0, "校验不过必须整条拒写，不得落任何 artifact"
            conn.close()

    def test_enum_validation(self) -> None:
        doc = _valid_doc(signals=[_valid_signal(type="gossip")])
        assert any("type" in error for error in radar_scan.validate_radar_scan(doc))
        for signal_type in radar_scan.SIGNAL_TYPES:
            assert radar_scan.validate_radar_scan(_valid_doc(signals=[_valid_signal(type=signal_type)])) == []
        doc = _valid_doc(signals=[_valid_signal(confidence="sure")])
        assert any("confidence" in error for error in radar_scan.validate_radar_scan(doc))
        doc = _valid_doc(signals=[_valid_signal(linked_action="auto_send")])
        assert any("linked_action" in error for error in radar_scan.validate_radar_scan(doc))

    def test_sanitize_drops_fabricated_urls_and_bad_enums(self) -> None:
        allowed = {"https://example.com/a", "https://example.com/b"}
        raw = [
            {"type": "risk", "summary": "公开平台显示欠薪投诉", "source_urls": ["https://evil.example.com/fake"]},
            {"type": "insider", "summary": "枚举外类型", "source_urls": ["https://example.com/a"]},
            {"type": "funding", "summary": "", "source_urls": ["https://example.com/a"]},
            {
                "type": "funding",
                "summary": "完成新一轮融资",
                "implication": "扩编窗口",
                "source_urls": ["https://example.com/a", "https://example.com/b"],
                "confidence": "very-high",
                "linked_action": "yolo",
                "as_of": "昨天",
            },
        ]
        signals, rejected = radar_scan.sanitize_signals(raw, company="示例公司", allowed_urls=allowed, as_of="2026-07-24")
        assert rejected["rejected_no_source"] == 1, "编造 URL 剥光后整条拒收"
        assert rejected["rejected_invalid"] == 2, "枚举外类型与空 summary 拒收"
        assert len(signals) == 1
        signal = signals[0]
        assert signal["confidence"] == "medium", "非法置信度落缺省"
        assert signal["linked_action"] == "activate", "非法动作落类型缺省（funding→activate）"
        assert signal["as_of"] == "2026-07-24", "非法日期回填扫描日"
        assert signal["source_urls"] == ["https://example.com/a", "https://example.com/b"]
        assert signal["company"] == "示例公司", "company 强制为被扫公司"


# ---------------------------------------------------------------------------
# 3. 同日幂等 upsert + 榜单 markdown 落盘
# ---------------------------------------------------------------------------

class UpsertIdempotencyTest(unittest.TestCase):
    def test_same_day_rescan_updates_same_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = _make_conn(Path(temp) / "m.db")
            radar_dir = Path(temp) / "radar"
            first = radar_scan.upsert_radar_scan(conn, _valid_doc(), radar_dir=radar_dir)
            doc2 = _valid_doc(signals=[_valid_signal(), _valid_signal(summary="第二条信号", source_urls=["https://example.com/news/2"])])
            doc2["stats"]["signals_found"] = 2
            second = radar_scan.upsert_radar_scan(conn, doc2, radar_dir=radar_dir)
            assert first == second == "radar_scan_2026-07-24", "同日重复扫描必须更新同一 artifact"
            rows = conn.execute("SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type='radar_scan'").fetchone()[0]
            assert rows == 1
            stored = json.loads(conn.execute("SELECT metadata_json FROM agent_artifacts").fetchone()[0])
            assert stored["version"] == 2 and len(stored["history"]) == 1
            assert len(stored["signals"]) == 2
            # 榜单 markdown 落盘（work/radar 口径；测试用临时目录）
            path = Path(stored["ranking_file"])
            assert path.is_file() and path.parent == radar_dir
            text = path.read_text(encoding="utf-8")
            assert "本周榜单" in text and "芯源微" in text and "https://example.com/news/1" in text
            assert "发起 Mapping 直挖" in text, "动作文案必须是业务语言"
            latest = radar_scan.get_latest_radar_scan(conn)
            assert latest is not None and latest["radar_scan"]["version"] == 2
            conn.close()

    def test_get_latest_empty_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            conn = _make_conn(Path(temp) / "m.db")
            assert radar_scan.get_latest_radar_scan(conn) is None
            conn.close()


# ---------------------------------------------------------------------------
# 4. 公司池 + 禁挖过滤 + 榜单打分（build_radar_scan 全链路，stub 采集/抽取）
# ---------------------------------------------------------------------------

class _StubCollector:
    def __init__(self, results_by_company: dict[str, list[dict[str, str]]]) -> None:
        self._results = results_by_company

    def collect_company(self, company: str) -> dict:
        return {"results": list(self._results.get(company, [])), "failures": []}


def _stub_extractor(llm, payload):
    results = payload.get("search_results") or []
    if not results:
        return {"signals": []}
    return {
        "signals": [
            {
                "type": "risk",
                "summary": f"{payload['company']} 公开报道出现风险事件",
                "implication": "团队可能观望",
                "source_urls": [results[0]["url"]],
                "as_of": payload["scan_date"],
                "confidence": "high",
                "linked_action": "mapping",
            }
        ]
    }


class BuildScanTest(unittest.TestCase):
    def _fixtures(self, temp: str) -> tuple[Path, Path, sqlite3.Connection]:
        root = Path(temp)
        profiles = root / "profiles"
        profiles.mkdir()
        (profiles / "杰华特_客户档案_v1.md").write_text("# 客户档案：杰华特", encoding="utf-8")
        (profiles / "士兰微_客户档案_v1.md").write_text("# 客户档案：士兰微", encoding="utf-8")
        kb = root / "kb"
        (kb / "cases").mkdir(parents=True)
        (kb / "cases" / "case_silan.json").write_text(
            json.dumps(
                {
                    "client_profile": {"name": "士兰微"},
                    "restricted": {
                        "banned_companies": ["杰华特"],
                        "consultant_phone": "13912345678",
                        "fee_rate": "费率23%",
                        "scripts_redline": "话术红线MARKER_REDLINE_S7",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conn = _make_conn(root / "m.db")
        conn.executescript(
            """
            CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,status TEXT,target_companies TEXT);
            """
        )
        conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
        conn.execute("INSERT INTO jobs VALUES (1,1,'技术市场经理','已发布/推进中','杰华特、晶丰明源')")
        conn.execute("INSERT INTO jobs VALUES (2,1,'已关岗位','已关闭','杰华特')")
        # mapping_task artifact：已确认团队公司 MPS（team_ref 0 候选 confirmed）
        mapping_doc = {
            "target_teams": [{"company": "MPS", "team": "电源团队"}, {"company": "矽力杰", "team": "模拟团队"}],
            "candidates": [{"team_ref": 0, "status": "confirmed"}, {"team_ref": 1, "status": "pending"}],
        }
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES ('mapping_task_wf1','g1','wf1',NULL,'mapping_task','t','text/markdown','',?,'passed')
            """,
            (json.dumps(mapping_doc, ensure_ascii=False),),
        )
        conn.commit()
        return profiles, kb, conn

    def test_pool_and_banned_filter_and_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profiles, kb, conn = self._fixtures(temp)
            pool, _trace = radar_scan.build_company_pool(conn, profiles_dir=profiles)
            names = [entry["company"] for entry in pool]
            assert names == ["士兰微", "杰华特", "MPS"], names  # 档案 2 家 + mapping 已确认 1 家（矽力杰未确认不进池）

            collector = _StubCollector(
                {
                    "杰华特": [{"title": "t", "url": "https://example.com/jw", "snippet": "s"}],
                    "MPS": [{"title": "t", "url": "https://example.com/mps", "snippet": "s"}],
                }
            )
            doc = radar_scan.build_radar_scan(
                conn,
                collector=collector,
                extractor=_stub_extractor,
                profiles_dir=profiles,
                kb_dir=kb,
                as_of="2026-07-24",
            )
            stats = doc["stats"]
            assert stats["companies_scanned"] == 3
            assert stats["banned_filtered"] == 1, "禁挖公司杰华特的信号必须过滤"
            assert stats["signals_found"] == 1, "过滤后只剩 MPS 一条"
            companies = {signal["company"] for signal in doc["signals"]}
            assert companies == {"MPS"}
            # 榜单：MPS 信号 risk×high=3.0；杰华特被过滤后不上榜（即便它有在手相关岗位）
            assert [entry["company"] for entry in doc["ranking"]] == ["MPS"]
            assert doc["ranking"][0]["score"] == 3.0
            # 在手岗位相关性可解释：杰华特虽被过滤，相关性匹配本身只认精确/别名口径
            jobs = radar_scan._open_jobs(conn)
            assert len(jobs) == 1, "已关闭岗位不计入在手岗位"
            assert [job["title"] for job in radar_scan.match_relevant_jobs("杰华特", jobs)] == ["技术市场经理"]
            assert radar_scan.match_relevant_jobs("长鑫存储", jobs) == []
            # restricted 不回泄：整个 doc 序列化后不含任何受限键值
            encoded = json.dumps(doc, ensure_ascii=False)
            for literal in FORBIDDEN_LITERALS:
                assert literal not in encoded, literal
            # 校验通过 + 落库
            assert radar_scan.validate_radar_scan(doc) == []
            conn.close()

    def test_collector_circuit_breaker_records_failures(self) -> None:
        calls: list[str] = []

        def dead_searcher(query: str, limit: int):
            calls.append(query)
            return [], "timeout"

        collector = radar_scan.RadarCollector(searcher=dead_searcher)
        first = collector.collect_company("甲公司")
        second = collector.collect_company("乙公司")
        assert first["failures"] and first["failures"][0]["reason"] == "timeout"
        assert second["failures"][0]["reason"] == "skipped_after_failure", "检索源熔断后逐公司跳过须留痕"
        assert len(calls) <= 2, "熔断后不得继续打满超时"

    def test_bing_parser_extracts_results(self) -> None:
        html = """
        <li class="b_algo"><h2><a href="https://example.com/news/1">芯源微发布业绩预告</a></h2>
        <div class="b_caption"><p>芯源微 2026 年半年度业绩预告显示……</p></div></li>
        <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9yZWRpci8y">重定向结果</a></h2>
        <p>摘要二</p></li>
        """
        results = radar_scan.parse_bing_results(html)
        assert [item["url"] for item in results] == ["https://example.com/news/1", "https://example.com/redir/2"]
        assert "业绩预告" in results[0]["title"] and "业绩预告" in results[0]["snippet"]


# ---------------------------------------------------------------------------
# 5. 路由 POST /api/v1/radar/scans + GET latest（200/404/幂等/同日更新）
# ---------------------------------------------------------------------------

class RadarApiTest(unittest.TestCase):
    def _create_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,status TEXT,target_companies TEXT);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,client TEXT,title TEXT);
            CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT);
            CREATE TABLE candidates(id INTEGER PRIMARY KEY,name TEXT,company TEXT,title TEXT,education TEXT,
              experience TEXT,skills TEXT,city TEXT,client TEXT,position TEXT,source TEXT,xsaas_id TEXT,
              search_date TEXT,status TEXT,notes TEXT,updated_at TEXT);
            CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,raw_client TEXT,
              raw_position TEXT,raw_status TEXT,raw_stage TEXT,clean_stage TEXT,flow_bucket TEXT,updated_at TEXT,
              source_candidate_id TEXT);
            CREATE TABLE candidate_events(id INTEGER PRIMARY KEY,job_candidate_id INTEGER,person_id INTEGER,job_id INTEGER,
              event_type TEXT,event_status TEXT,event_time TEXT,summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
            CREATE TABLE source_profiles(id INTEGER PRIMARY KEY,person_id INTEGER,source_type TEXT,
              source_candidate_id TEXT,source_date TEXT,raw_status TEXT,raw_client TEXT,raw_position TEXT,raw_json TEXT);
            """
        )
        conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
        conn.execute("INSERT INTO jobs VALUES (1,1,'技术市场经理','已发布/推进中','MPS')")
        conn.commit()
        conn.close()

    def _kb_fixture(self, root: Path) -> Path:
        kb = root / "kb"
        (kb / "client_profiles_public_v1").mkdir(parents=True)
        (kb / "client_profiles_public_v1" / "士兰微_客户档案_v1.md").write_text("# 档案", encoding="utf-8")
        (kb / "client_profiles_public_v1" / "杰华特_客户档案_v1.md").write_text("# 档案", encoding="utf-8")
        (kb / "cases").mkdir(parents=True)
        (kb / "cases" / "case_silan.json").write_text(
            json.dumps(
                {
                    "client_profile": {"name": "士兰微"},
                    "restricted": {
                        "banned_companies": ["杰华特"],
                        "consultant_phone": "13912345678",
                        "fee_rate": "费率23%",
                        "scripts_redline": "话术红线MARKER_REDLINE_S7",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return kb

    def test_post_get_latest_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "asa.db"
            self._create_db(db_path)
            kb = self._kb_fixture(root)
            radar_dir = root / "radar"
            app = create_app(db_path=db_path, start_legacy=False)

            collector = _StubCollector(
                {
                    "士兰微": [{"title": "t", "url": "https://example.com/silan", "snippet": "s"}],
                    "杰华特": [{"title": "t", "url": "https://example.com/jw", "snippet": "s"}],
                }
            )
            with (
                TestClient(app) as client,
                mock.patch.dict(os.environ, {"ASA_KNOWLEDGE_BASE_DIR": str(kb)}),
                mock.patch.object(radar_scan, "RadarCollector", lambda: collector),
                mock.patch.object(radar_scan, "llm_extract_signals", _stub_extractor),
                mock.patch.object(radar_scan, "default_radar_dir", lambda: radar_dir),
            ):
                missing = client.get("/api/v1/radar/scans/latest")
                assert missing.status_code == 404, "尚无扫描必须 404"

                first = client.post(
                    "/api/v1/radar/scans",
                    json={"request_id": "req-radar-1"},
                    headers={"Idempotency-Key": "k-radar-1"},
                )
                assert first.status_code == 200, first.text
                payload = first.json()
                assert payload["ok"] is True and payload["artifact_id"].startswith("radar_scan_")
                assert payload["receipt"]["idempotent_replay"] is False
                doc = payload["radar_scan"]
                assert doc["schema_version"] == "radar_v1"
                assert doc["stats"]["companies_scanned"] == 2
                assert doc["stats"]["banned_filtered"] == 1, "杰华特信号必须被禁挖过滤"
                for signal in doc["signals"]:
                    assert signal["source_urls"], "编造检查：每条信号必须带 ≥1 来源 URL"
                    assert signal["type"] in radar_scan.SIGNAL_TYPES
                assert Path(payload["ranking_file"]).is_file(), "榜单 markdown 必须落盘"
                encoded = json.dumps(payload, ensure_ascii=False)
                for literal in FORBIDDEN_LITERALS:
                    assert literal not in encoded, literal

                replay = client.post(
                    "/api/v1/radar/scans",
                    json={"request_id": "req-radar-1"},
                    headers={"Idempotency-Key": "k-radar-1"},
                )
                assert replay.status_code == 200
                assert replay.json()["receipt"]["idempotent_replay"] is True, "同键重放首次响应"

                # 同日新一次扫描（新幂等键）：更新同一 artifact，version 自增，不重复建行
                second = client.post(
                    "/api/v1/radar/scans",
                    json={"request_id": "req-radar-2"},
                    headers={"Idempotency-Key": "k-radar-2"},
                )
                assert second.status_code == 200, second.text
                assert second.json()["artifact_id"] == payload["artifact_id"]
                assert second.json()["radar_scan"]["version"] == 2

                latest = client.get("/api/v1/radar/scans/latest")
                assert latest.status_code == 200, latest.text
                detail = latest.json()
                assert detail["ok"] is True
                assert detail["radar_scan"]["version"] == 2
                assert detail["artifact_id"] == payload["artifact_id"]
                assert "本周榜单" in detail["content"]

                conn = sqlite3.connect(db_path)
                rows = conn.execute("SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type='radar_scan'").fetchone()[0]
                conn.close()
                assert rows == 1, "同日重复扫描不得重复建行"


if __name__ == "__main__":
    unittest.main()
