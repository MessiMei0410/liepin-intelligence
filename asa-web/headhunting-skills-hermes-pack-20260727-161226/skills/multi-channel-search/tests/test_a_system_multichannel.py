import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "a_system_multichannel.py"


def load_module():
    spec = importlib.util.spec_from_file_location("a_system_multichannel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PositionContextTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE clients(id INTEGER, name TEXT);
            CREATE TABLE jobs(id INTEGER, client_id INTEGER, title TEXT, status TEXT);
            CREATE TABLE positions(
                id INTEGER, client TEXT, title TEXT, status TEXT, location TEXT,
                experience TEXT, hard_requirements TEXT, ability_keywords TEXT,
                target_companies TEXT, exclusions TEXT, search_words TEXT, summary TEXT
            );
            CREATE TABLE position_profiles(
                id INTEGER, client TEXT, position TEXT,
                education_requirement TEXT, experience_requirement TEXT,
                hard_requirements_json TEXT, ability_keywords_json TEXT,
                target_companies_json TEXT, exclusion_tags_json TEXT,
                search_keywords_json TEXT, source_position_ids_json TEXT,
                soft_preferences_json TEXT, pitch_points_json TEXT,
                risk_points_json TEXT, jd_analysis_summary TEXT
            );
            CREATE TABLE candidates(
                id INTEGER, name TEXT, company TEXT, title TEXT, client TEXT,
                position TEXT, status TEXT, source TEXT, xsaas_id TEXT,
                elimination_reason TEXT, education TEXT, experience TEXT,
                skills TEXT, level TEXT, city TEXT, search_date TEXT, notes TEXT,
                iteration INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE people(
                id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT,
                current_company TEXT, current_title TEXT, city TEXT,
                education TEXT, experience TEXT, fingerprint TEXT UNIQUE,
                created_at TEXT
            );
            CREATE TABLE job_candidates(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER,
                person_id INTEGER, raw_client TEXT, raw_position TEXT,
                raw_status TEXT, raw_stage TEXT, clean_stage TEXT,
                flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER,
                search_date TEXT, updated_at TEXT, source_candidate_id TEXT
            );
            CREATE TABLE candidate_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER,
                person_id INTEGER, job_id INTEGER, event_type TEXT,
                event_status TEXT, event_time TEXT, summary TEXT,
                raw_json TEXT, source_table TEXT, source_id TEXT
            );
            CREATE TABLE source_profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER,
                source_type TEXT, source_candidate_id TEXT, source_date TEXT,
                raw_status TEXT, raw_client TEXT, raw_position TEXT,
                raw_json TEXT
            );
            CREATE TABLE candidate_clients(
                id INTEGER, candidate_name TEXT, candidate_company TEXT,
                client TEXT, source TEXT, position_tag TEXT, created_at TEXT
            );
            CREATE TABLE candidate_profiles(
                id INTEGER, candidate_id INTEGER, candidate_name TEXT,
                candidate_company TEXT, client TEXT, position TEXT,
                education_level TEXT, seniority TEXT, industry_tags_json TEXT,
                function_tags_json TEXT, risk_tags_json TEXT,
                profile_summary TEXT, updated_at TEXT
            );
            CREATE TABLE candidate_intelligence(
                id INTEGER, candidate_id INTEGER, candidate_name TEXT,
                candidate_company TEXT, client TEXT, position TEXT,
                fit_score INTEGER, fit_level TEXT, evidence_json TEXT,
                risk_json TEXT, next_action TEXT, last_evaluated_at TEXT,
                model_version TEXT, created_at TEXT, updated_at TEXT,
                strong_matches_json TEXT, weak_matches_json TEXT,
                verification_questions_json TEXT,
                recommendation_decision TEXT
            );
            CREATE TABLE search_experiments(
                id INTEGER, client TEXT, position TEXT, channel TEXT,
                query TEXT, recommended_count INTEGER, status TEXT,
                run_time TEXT
            );
            INSERT INTO clients VALUES (1, '长越科技');
            INSERT INTO jobs VALUES (134, 1, '自动化软件高级工程师', '已发布');
            INSERT INTO positions VALUES (
                5155, '长越科技', '自动化软件高级工程师', 'open', '杭州', '5年以上',
                '本科；5年以上实时运动控制', 'EtherCAT; TwinCAT; C++',
                '华卓精科; 上海微电子', '纯上位机; MES',
                '实时运动控制 EtherCAT C++; 多轴运动控制 RTOS', '精密设备实时控制软件'
            );
            INSERT INTO position_profiles VALUES (
                9001, '长越科技', '自动化软件高级工程师', '本科', '5年以上',
                '["本科", "5年以上实时运动控制"]',
                '["EtherCAT", "TwinCAT", "C++"]',
                '["华卓精科", "上海微电子"]',
                '["纯上位机", "MES"]',
                '["实时运动控制 EtherCAT C++", "多轴运动控制 RTOS"]',
                '[5155]', '[]', '["精密设备核心岗"]', '["缺少实时控制"]',
                '精密设备实时控制软件'
            );
            INSERT INTO candidates
                (id,name,company,title,client,position,status,source,xsaas_id,
                 elimination_reason)
            VALUES
                (1001, '赵**', '派克汉尼汾流体传动产品(上海)有限公司',
                 '自动化工程师', '长越科技', '自动化软件高级工程师',
                 'contacted', 'liepin', '', ''),
                (1002, '刘韩斌', '科远智慧', 'C#软件开发工程师',
                 '长越科技', '自动化软件高级工程师',
                 'contacted', 'xsaas', '4294604', '方向不符');
            INSERT INTO people
                (id,display_name,current_company,current_title,fingerprint)
            VALUES
                (501, '赵**', '派克汉尼汾流体传动产品(上海)有限公司',
                 '自动化工程师', '赵**|派克汉尼汾流体传动产品(上海)有限公司|自动化工程师'),
                (502, '刘韩斌', '科远智慧', 'C#软件开发工程师',
                 '刘韩斌|科远智慧|c#软件开发工程师');
            INSERT INTO job_candidates
                (id,job_id,person_id,raw_position,clean_stage,source_candidate_id)
            VALUES
                (701, 134, 501, '自动化软件高级工程师', '已触达', '1001'),
                (702, 134, 502, '自动化软件高级工程师', '已触达', '1002');
            INSERT INTO candidate_events
                (id,job_candidate_id,event_type,event_status,event_time)
            VALUES
                (801, 702, 'resume_review_completed', 'continue', '2026-07-09 18:30:50'),
                (802, 702, 'resume_review_completed', 'stop', '2026-07-09 18:39:37');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_load_position_context_uses_canonical_open_position(self):
        module = load_module()

        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        self.assertEqual(context["job_id"], 134)
        self.assertEqual(context["position_id"], 5155)
        self.assertEqual(context["client"], "长越科技")
        self.assertEqual(context["job"], "自动化软件高级工程师")
        self.assertEqual(context["location"], "杭州")
        self.assertEqual(context["search_keywords"][0], "实时运动控制 EtherCAT C++")
        self.assertEqual(context["source_position_ids"], [5155])

    def test_load_position_context_rejects_missing_job(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "未找到在招岗位"):
            module.load_position_context(self.db_path, "长越科技", "不存在岗位")

    def test_load_position_context_accepts_a_system_active_status(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE positions SET status='已发布'")
        conn.commit()
        conn.close()
        module = load_module()

        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        self.assertEqual(context["position_id"], 5155)

    def test_load_position_context_accepts_legacy_job_id_profile_binding(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE position_profiles SET source_position_ids_json='[134]'"
        )
        conn.commit()
        conn.close()
        module = load_module()

        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        self.assertEqual(context["source_binding"], "job_id_legacy")

    def test_load_position_context_rejects_profile_bound_to_other_position(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE position_profiles SET source_position_ids_json='[9999]'"
        )
        conn.commit()
        conn.close()
        module = load_module()

        with self.assertRaisesRegex(ValueError, "画像来源岗位不匹配"):
            module.load_position_context(
                self.db_path, "长越科技", "自动化软件高级工程师"
            )


class ExclusionSetTests(PositionContextTests):
    def test_load_exclusion_set_preserves_latest_manual_stop(self):
        module = load_module()

        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        by_id = {row["candidate_id"]: row for row in exclusion["records"]}
        self.assertEqual(by_id[1001]["disposition"], "contacted")
        self.assertEqual(by_id[1002]["disposition"], "stopped")
        self.assertEqual(exclusion["summary"]["stopped"], 1)
        self.assertEqual(exclusion["summary"]["contacted"], 1)

    def test_classify_duplicate_matches_local_candidate_id(self):
        module = load_module()
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        result = module.classify_duplicate({"candidate_id": 1001}, exclusion)

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["reason"], "local_candidate_id")

    def test_classify_duplicate_matches_xsaas_id(self):
        module = load_module()
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        result = module.classify_duplicate({"xsaas_id": "4294604"}, exclusion)

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["disposition"], "stopped")

    def test_classify_duplicate_matches_masked_name_with_company_and_title(self):
        module = load_module()
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        result = module.classify_duplicate(
            {
                "name": "赵**",
                "company": "派克汉尼汾流体传动产品（上海）有限公司",
                "title": "自动化工程师",
            },
            exclusion,
        )

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["reason"], "identity_evidence")

    def test_classify_duplicate_does_not_match_masked_name_alone(self):
        module = load_module()
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        result = module.classify_duplicate({"name": "赵**"}, exclusion)

        self.assertFalse(result["duplicate"])


class SearchPlanTests(PositionContextTests):
    def test_build_search_plan_is_job_driven_and_deterministic(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        plan = module.build_search_plan(context, exclusion, max_queries_per_channel=4)

        self.assertEqual(plan["mode"], "a-system-job")
        self.assertEqual(plan["job_id"], 134)
        self.assertLessEqual(len(plan["channels"]["liepin"]), 4)
        self.assertLessEqual(len(plan["channels"]["xsaas"]), 4)
        self.assertEqual(
            len(plan["channels"]["liepin"]),
            len({row["query"] for row in plan["channels"]["liepin"]}),
        )
        self.assertIn("纯上位机", plan["review_gates"]["negative_rules"])
        self.assertIn("方向不符", plan["review_gates"]["historical_stop_reasons"])
        self.assertTrue(
            any("华卓精科" in row["query"] for row in plan["channels"]["liepin"])
        )

    def test_build_search_plan_skips_exhausted_zero_yield_query(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        history = [
            {
                "query": "实时运动控制 EtherCAT C++",
                "status": "learned",
                "recommended_count": 0,
            }
        ]

        plan = module.build_search_plan(
            context, exclusion, history, max_queries_per_channel=6
        )

        queries = {
            row["query"]
            for channel in plan["channels"].values()
            for row in channel
        }
        self.assertNotIn("实时运动控制 EtherCAT C++", queries)
        self.assertIn("实时运动控制 EtherCAT C++", plan["skipped_queries"])


class ChannelPreflightTests(unittest.TestCase):
    def test_liepin_ready_requires_submitted_query_and_relevant_cards(self):
        module = load_module()

        result = module.classify_channel_snapshot(
            "liepin",
            {
                "href": "https://h.liepin.com/search/getConditionItem#session",
                "title": "找简历",
                "input_value": "TwinCAT 半导体设备 运动控制",
                "total": "92",
                "card_count": 30,
                "relevant_card_count": 8,
            },
            expected_query="TwinCAT 半导体设备 运动控制",
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")

    def test_liepin_generic_feed_is_blocked(self):
        module = load_module()

        result = module.classify_channel_snapshot(
            "liepin",
            {
                "href": "https://h.liepin.com/search/getConditionItem#session",
                "title": "找简历",
                "input_value": "",
                "total": "3000+",
                "card_count": 30,
                "relevant_card_count": 0,
            },
            expected_query="ACS 控制器 半导体设备 C++",
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "invalid_search")

    def test_xsaas_login_page_is_not_zero_results(self):
        module = load_module()

        result = module.classify_channel_snapshot(
            "xsaas",
            {
                "href": "https://headhunt.x-saas.com.cn/#/login",
                "title": "X-SaaS智能招聘系统",
                "candidate_count": 0,
            },
            expected_query="半导体设备 C++ 软件工程师",
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "login_required")
        self.assertNotEqual(result["status"], "zero_results")

    def test_xsaas_cached_query_is_blocked(self):
        module = load_module()

        result = module.classify_channel_snapshot(
            "xsaas",
            {
                "href": (
                    "https://headhunt.x-saas.com.cn/#/app/candidate/list?"
                    "SearchKeyWords=old-query"
                ),
                "title": "候选人列表",
                "candidate_count": 20,
            },
            expected_query="TwinCAT 运动控制",
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "stale_query")


class CandidateNormalizationTests(PositionContextTests):
    def test_normalize_liepin_candidate_stages_s1_pending_review(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        candidate = module.normalize_candidate(
            "liepin",
            {
                "name": "周**",
                "current_company": "某精密设备有限公司",
                "current_position": "运动控制软件工程师",
                "education": "本科",
                "experience": "工作8年",
                "location": "杭州",
                "profile_text": "C++ EtherCAT 多轴运动控制",
                "resume_url": "https://h.liepin.com/resume/showresumedetail/?x=1",
            },
            context,
        )

        self.assertEqual(candidate["client"], "长越科技")
        self.assertEqual(candidate["job"], "自动化软件高级工程师")
        self.assertEqual(candidate["stage"], "S1 新增寻访/待复核")
        self.assertEqual(candidate["event_status"], "pending_review")
        self.assertEqual(candidate["company"], "某精密设备有限公司")

    def test_normalize_xsaas_candidate_stages_x1_pending_review(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        candidate = module.normalize_candidate(
            "xsaas",
            {
                "name": "周明",
                "current_company": "某精密设备有限公司",
                "current_position": "运动控制软件工程师",
                "candidate_id": "5566778",
            },
            context,
        )

        self.assertEqual(candidate["stage"], "X1 X-SaaS新增/待复核")
        self.assertEqual(candidate["xsaas_id"], "5566778")
        self.assertEqual(candidate["source"], "xsaas")

    def test_normalize_prefers_captured_detail_sections_over_card_history(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        candidate = module.normalize_candidate(
            "liepin",
            {
                "name": "周**",
                "company": "某精密设备有限公司",
                "title": "运动控制软件工程师",
                "profile_text": "完整候选人履历 " * 20,
                "full_text": "完整候选人履历 " * 20,
                "work_text": "某精密设备有限公司\n2020.01-至今\n运动控制软件工程师\n负责EtherCAT多轴控制",
                "project_text": "晶圆传输运动控制项目\n负责实时控制架构",
                "education_text": "浙江大学\n自动化\n本科\n2012.09-2016.06",
                "resume_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=detail-1",
                "res_id_encode": "detail-1",
                "resume_capture_status": "complete",
                "work": [{"company": "卡片公司", "title": "卡片职位", "dates": "2020-至今"}],
            },
            context,
        )

        self.assertIn("负责EtherCAT多轴控制", candidate["work_text"])
        self.assertIn("晶圆传输运动控制项目", candidate["project_text"])
        self.assertIn("浙江大学", candidate["education_text"])

    def test_normalize_rejects_pipeline_record_when_full_resume_capture_is_partial(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        with self.assertRaisesRegex(ValueError, "完整简历未通过入库校验"):
            module.normalize_candidate(
                "liepin",
                {
                    "name": "周**",
                    "company": "某精密设备有限公司",
                    "title": "运动控制软件工程师",
                    "profile_text": "只有搜索卡片摘要",
                    "resume_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=detail-2",
                    "res_id_encode": "detail-2",
                    "resume_capture_status": "partial",
                    "resume_capture_missing": ["工作经历", "教育经历"],
                },
                context,
            )

    def test_normalize_candidate_rejects_school_as_company(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )

        with self.assertRaisesRegex(ValueError, "公司字段疑似学校"):
            module.normalize_candidate(
                "liepin",
                {
                    "name": "李**",
                    "company": "江苏科技大学",
                    "title": "软件工程师",
                },
                context,
            )

    def test_stage_candidates_separates_existing_and_batch_duplicates(self):
        module = load_module()
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        exclusion = module.load_exclusion_set(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        records = [
            {
                "channel": "liepin",
                "name": "赵**",
                "company": "派克汉尼汾流体传动产品（上海）有限公司",
                "title": "自动化工程师",
            },
            {
                "channel": "xsaas",
                "name": "周明",
                "company": "某精密设备有限公司",
                "title": "运动控制软件工程师",
                "candidate_id": "5566778",
            },
            {
                "channel": "xsaas",
                "name": "周明",
                "company": "某精密设备有限公司",
                "title": "运动控制软件工程师",
                "candidate_id": "5566778",
            },
        ]

        staged = module.stage_candidates(records, context, exclusion)

        self.assertEqual(len(staged["accepted"]), 1)
        self.assertEqual(len(staged["existing"]), 1)
        self.assertEqual(len(staged["batch_duplicates"]), 1)
        self.assertEqual(staged["accepted"][0]["name"], "周明")


class IntakeTests(PositionContextTests):
    def _new_candidate(self, module):
        context = module.load_position_context(
            self.db_path, "长越科技", "自动化软件高级工程师"
        )
        return context, module.normalize_candidate(
            "liepin",
            {
                "name": "周**",
                "company": "某精密设备有限公司",
                "title": "运动控制软件工程师",
                "education": "本科",
                "experience": "工作8年",
                "city": "杭州",
                "profile_text": "C++ EtherCAT 多轴运动控制",
                "query": "实时运动控制 EtherCAT C++",
                "resume_url": "https://h.liepin.com/resume/showresumedetail/?x=1",
                "res_id_encode": "lp-session-123",
            },
            context,
        )

    def test_apply_intake_dry_run_does_not_mutate_database(self):
        module = load_module()
        context, candidate = self._new_candidate(module)
        conn = sqlite3.connect(self.db_path)
        before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        conn.close()

        result = module.apply_intake(
            self.db_path, context, [candidate], apply=False
        )

        conn = sqlite3.connect(self.db_path)
        after = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        conn.close()
        self.assertFalse(result["applied"])
        self.assertEqual(result["planned"], 1)
        self.assertEqual(before, after)

    def test_apply_intake_writes_all_required_a_system_surfaces(self):
        module = load_module()
        context, candidate = self._new_candidate(module)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE entity_source_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_type TEXT NOT NULL,
                canonical_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                source_entity_type TEXT NOT NULL,
                source_entity_id TEXT NOT NULL,
                source_url TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(source_system,source_entity_type,source_entity_id,canonical_type,canonical_id)
            )
            """
        )
        conn.commit()
        conn.close()

        result = module.apply_intake(
            self.db_path, context, [candidate], apply=True
        )

        self.assertTrue(result["applied"])
        self.assertEqual(result["inserted"], 1)
        conn = sqlite3.connect(self.db_path)
        candidate_row = conn.execute(
            "SELECT id,status FROM candidates WHERE name='周**'"
        ).fetchone()
        source_id = conn.execute(
            "SELECT source_candidate_id FROM job_candidates WHERE person_id>502"
        ).fetchone()[0]
        stage = conn.execute(
            "SELECT clean_stage FROM job_candidates WHERE person_id>502"
        ).fetchone()[0]
        event = conn.execute(
            "SELECT event_status,source_id FROM candidate_events WHERE id>802"
        ).fetchone()
        source_link = conn.execute(
            "SELECT source_system,source_url FROM entity_source_links"
        ).fetchone()
        source_profile = conn.execute(
            "SELECT source_type,source_candidate_id,raw_json FROM source_profiles WHERE person_id>502"
        ).fetchone()
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "candidate_clients",
                "candidate_profiles",
                "candidate_intelligence",
            )
        }
        conn.close()
        self.assertEqual(candidate_row[1], "new")
        self.assertEqual(source_id, str(candidate_row[0]))
        self.assertEqual(stage, "S1 新增寻访/待复核")
        self.assertEqual(event[0], "pending_review")
        self.assertIn("showresumedetail", event[1])
        self.assertEqual(source_link[0], "liepin")
        self.assertIn("showresumedetail", source_link[1])
        self.assertEqual(source_profile[0], "liepin")
        self.assertEqual(source_profile[1], "lp-session-123")
        self.assertEqual(json.loads(source_profile[2])["profile_text"], "C++ EtherCAT 多轴运动控制")
        self.assertIn("showresumedetail", json.loads(source_profile[2])["source_url"])
        self.assertEqual(counts, {
            "candidate_clients": 1,
            "candidate_profiles": 1,
            "candidate_intelligence": 1,
        })

    def test_apply_intake_is_idempotent_for_same_identity(self):
        module = load_module()
        context, candidate = self._new_candidate(module)
        module.apply_intake(self.db_path, context, [candidate], apply=True)

        second = module.apply_intake(
            self.db_path, context, [candidate], apply=True
        )

        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["skipped_existing"], 1)


class CliTests(PositionContextTests):
    def test_run_cli_context_returns_canonical_job(self):
        module = load_module()

        result = module.run_cli(
            [
                "context",
                "--db",
                str(self.db_path),
                "--client",
                "长越科技",
                "--job",
                "自动化软件高级工程师",
            ]
        )

        self.assertEqual(result["job_id"], 134)
        self.assertEqual(result["position_id"], 5155)

    def test_run_cli_plan_uses_search_history(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO search_experiments VALUES "
            "(1,'长越科技','自动化软件高级工程师','liepin',"
            "'实时运动控制 EtherCAT C++',0,'learned','2026-07-10')"
        )
        conn.commit()
        conn.close()
        module = load_module()

        result = module.run_cli(
            [
                "plan",
                "--db",
                str(self.db_path),
                "--client",
                "长越科技",
                "--job",
                "自动化软件高级工程师",
            ]
        )

        self.assertIn("实时运动控制 EtherCAT C++", result["skipped_queries"])

    def test_run_cli_intake_is_dry_run_without_apply(self):
        input_path = Path(self.tempdir.name) / "candidates.json"
        input_path.write_text(
            json.dumps(
                [
                    {
                        "channel": "xsaas",
                        "name": "周明",
                        "company": "某精密设备有限公司",
                        "title": "运动控制软件工程师",
                        "candidate_id": "5566778",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        module = load_module()

        result = module.run_cli(
            [
                "intake",
                "--db",
                str(self.db_path),
                "--client",
                "长越科技",
                "--job",
                "自动化软件高级工程师",
                "--input",
                str(input_path),
            ]
        )

        self.assertFalse(result["intake"]["applied"])
        self.assertEqual(result["staged"]["accepted_count"], 1)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
