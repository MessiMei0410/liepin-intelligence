"""P3-c：技能本体扩 fab 工艺/质量/YE/FPGA 四族的加载与归一测试。

覆盖：
- 真实 kb_skill_ontology_semiconductor_v1.json 扩族后为九族，四族新族齐备；
- 新族每条技能带别名/相关/证据（沿用 test_skill_ontology_level_mapping 的结构口径）；
- 新族技能词别名归一（中英文/缩写命中，source=kb_skill）与相关词提示；
- 四个 seed 原型的 skills_ontology_nodes 回填词全部是本体 canonical 名。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from a_system_agent import knowledge_base

NEW_FAMILIES = {
    "fab_process": "晶圆制造工艺与整合",
    "quality": "质量与可靠性",
    "yield_enhancement": "良率提升与缺陷分析",
    "fpga": "FPGA 与嵌入式硬件",
}

SEED_BACKFILL_FILES = (
    "seed_pengxinxu_fab_process_v1.json",
    "seed_pengxinxu_fab_quality_v1.json",
    "seed_pengxinxu_fab_yield_v1.json",
    "seed_sukesi_fpga_v1.json",
)


def _kb_dir() -> Path:
    from a_system_agent.strategy_v2 import knowledge_base_dir

    return knowledge_base_dir()


class FourFamiliesLoadTest(unittest.TestCase):
    """真实本体扩族后：九族载入、无降级告警、四族新族节点结构完整。"""

    def setUp(self) -> None:
        self.ontology, self.trace = knowledge_base.load_skill_ontology()

    def test_nine_families_loaded(self) -> None:
        assert len(self.ontology["families"]) == 9, self.ontology["families"]
        for family_id, label in NEW_FAMILIES.items():
            assert self.ontology["families"].get(family_id) == label
        assert any("已加载技能本体" in line for line in self.trace)
        assert not any("降级" in line for line in self.trace), self.trace

    def test_new_family_nodes_complete(self) -> None:
        counts: dict[str, int] = {family_id: 0 for family_id in NEW_FAMILIES}
        for name, skill in self.ontology["skills"].items():
            if skill["family"] not in NEW_FAMILIES:
                continue
            counts[skill["family"]] += 1
            assert skill["aliases"], f"{name} 缺别名"
            assert skill["related"], f"{name} 缺相关技能"
            assert skill["evidence"], f"{name} 缺证据形式"
        for family_id, count in counts.items():
            assert count >= 6, f"{family_id} 技能节点过少（{count}）"

    def test_business_core_terms_present(self) -> None:
        for expected in (
            "工艺整合", "光刻", "刻蚀", "CMP", "SPC", "FMEA", "8D",
            "良率提升", "失效分析", "FPGA", "Verilog", "时序约束",
        ):
            assert expected in self.ontology["skills"], f"{expected} 应在本体内"


class FourFamiliesNormalizeTest(unittest.TestCase):
    """新族技能词别名归一与相关词提示。"""

    def setUp(self) -> None:
        self.ontology, _ = knowledge_base.load_skill_ontology()

    def test_alias_hits(self) -> None:
        cases = [
            ("PIE", "工艺整合", "fab_process"),
            ("化学机械抛光", "CMP", "fab_process"),
            ("plasma etch", "刻蚀", "fab_process"),
            ("8d", "8D", "quality"),
            ("统计过程控制", "SPC", "quality"),
            ("yield enhancement", "良率提升", "yield_enhancement"),
            ("failure analysis", "失效分析", "yield_enhancement"),
            ("SystemVerilog", "Verilog", "fpga"),
            ("紫光同创", "FPGA器件", "fpga"),
        ]
        for term, canonical, family in cases:
            info = knowledge_base.normalize_skill(term, self.ontology)
            assert info["matched"] is True, f"{term} 未命中"
            assert info["canonical"] == canonical, f"{term} -> {info['canonical']}，期望 {canonical}"
            assert info["family"] == family, f"{term} 落族 {info['family']}，期望 {family}"
            assert info["source"] == "kb_skill"

    def test_related_skills_prompt(self) -> None:
        related = knowledge_base.related_skills("FMEA", self.ontology)
        assert "8D" in related


class SeedBackfillTest(unittest.TestCase):
    """四个 seed 原型 skills_ontology_nodes 回填词全部是本体 canonical 名。"""

    def test_backfilled_nodes_are_canonical(self) -> None:
        ontology, _ = knowledge_base.load_skill_ontology()
        canonical = set(ontology["skills"])
        kb = _kb_dir()
        for fname in SEED_BACKFILL_FILES:
            path = kb / fname
            assert path.is_file(), f"{fname} 缺失"
            doc = json.loads(path.read_text(encoding="utf-8"))
            nodes = doc.get("skills_ontology_nodes")
            assert isinstance(nodes, list) and nodes, f"{fname} skills_ontology_nodes 仍为空"
            unknown = [node for node in nodes if node not in canonical]
            assert not unknown, f"{fname} 回填词不在本体 canonical：{unknown}"


if __name__ == "__main__":
    unittest.main()
