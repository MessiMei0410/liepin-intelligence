"""S4-3：排除规则引擎 —— 策略生成第 4 步之后强制过五类检查清单。

口径来源（事实源，运行时只读；目录可用 ASA_KNOWLEDGE_BASE_DIR 覆盖）：
- docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §4（五类检查清单）
- knowledge_base/kb_seed_jiachi_equipment_v1.json 的 negative_rule_typology（五类 typology）

五类（顺序固定，与 PRD §4 一致）：
1. 在职保护名单（客户级禁挖名单合并；restricted 层按客户读取，同客户新岗位自动继承）
2. 学历门槛（依据：岗位硬性要求 / 顾问补充 / KB 原型规则）
3. 身份/背景限制（依据：岗位硬性要求 / 顾问补充 / KB 原型规则）
4. 竞业协议排除（依据：restricted 层竞业约束 / 岗位文本）
5. 稳定性筛选（依据：岗位硬性要求 / 顾问补充 / KB 原型规则，如"五年三跳"）

每类输出 {type, applicable, rule, basis, source}——"适用/不适用 + 依据"逐类留痕，
由运行时并入 strategy_v2 的 negative_rules。无依据的类标 applicable=false 并给出理由。

P0 边界：restricted 层只经 knowledge_base.load_restricted_constraints 白名单出库
（禁挖名单/竞业限制），本模块不直接读取 restricted 文件；费率/手机号/offer 金额/
话术红线永远不会出现在本模块的任何输出里。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .strategy_v2 import knowledge_base_dir

# 五类检查清单（PRD §4 顺序；typology 以 KB 为准，缺失时按此默认清单降级）
NEGATIVE_RULE_TYPES = ("在职保护名单", "学历门槛", "身份/背景限制", "竞业协议排除", "稳定性筛选")

TYPOLOGY_GLOB = "kb_seed_*.json"

# 各类别的判定信号词（可解释：命中哪个词写进依据）
_EDUCATION_TOKENS = ("统招", "全日制", "本科", "硕士", "博士", "一本", "二本", "学历", "985", "211")
_IDENTITY_TOKENS = ("台湾人", "台湾籍", "国籍", "外籍", "身份限制", "身份背景", "背景限制", "政审", "户口")
_NON_COMPETE_TOKENS = ("竞业",)
_STABILITY_TOKENS = ("五年三跳", "三年两跳", "频繁跳槽", "频繁换工作", "跳槽", "稳定性")

_JOB_TEXT_FIELDS = (
    "title", "summary", "hard_requirements", "requirements", "responsibilities",
    "exclusions", "education", "experience", "ability_keywords",
)

# 岗位画像（position_profiles）中同样属于岗位硬性要求/排除口径的字段
_PROFILE_TEXT_FIELDS = (
    "education_requirement", "experience_requirement", "hard_requirements_json",
    "exclusion_tags_json", "risk_points_json",
)

_NON_COMPETE_KEY_PATTERN = re.compile(r"竞业|non_?compete", re.I)


def load_negative_rule_typology(kb_dir: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 kb_seed_*.json 的 negative_rule_typology.types（五类负向规则分类）。

    文件缺失/解析失败一律降级为 PRD §4 默认五类清单并留痕（不抛异常、不写文件）。
    返回 (types[{type, example, ...}], trace)。
    """
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    trace: list[str] = []
    types: list[dict[str, Any]] = []
    if not directory.is_dir():
        trace.append(f"知识库目录不存在：{directory}，五类清单按 PRD §4 默认分类降级")
        return _default_typology(), trace
    for path in sorted(directory.glob(TYPOLOGY_GLOB)):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            trace.append(f"{path.name} 解析失败（{exc.__class__.__name__}），跳过该 typology")
            continue
        raw = doc.get("negative_rule_typology") if isinstance(doc, dict) else None
        entries = raw.get("types") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("type") or "").strip():
                types.append(
                    {
                        "type": str(entry.get("type") or ""),
                        "example": str(entry.get("example") or ""),
                        "note": str(entry.get("note") or ""),
                        "source_file": path.name,
                    }
                )
        if types:
            trace.append(f"已加载负向规则 typology {len(types)} 类（{path.name}）")
    if not types:
        trace.append(f"知识库 {directory} 无 {TYPOLOGY_GLOB} 负向规则 typology，按 PRD §4 默认五类降级")
        return _default_typology(), trace
    # KB typology 为事实源，但五类清单口径固定：KB 缺的类按默认补齐，多出的类追加留痕
    known = {entry["type"] for entry in types}
    for default in _default_typology():
        if default["type"] not in known:
            types.append(default)
            trace.append(f"typology 缺“{default['type']}”类，按 PRD §4 默认补齐")
    order = {name: index for index, name in enumerate(NEGATIVE_RULE_TYPES)}
    types.sort(key=lambda entry: (order.get(entry["type"], len(order)), entry["type"]))
    return types, trace


def _default_typology() -> list[dict[str, Any]]:
    return [{"type": name, "example": "", "note": "PRD §4 默认分类", "source_file": ""} for name in NEGATIVE_RULE_TYPES]


def _segments(text: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"[\n。；;]+", str(text or "")) if segment.strip()]


def _hits(segments: list[str], tokens: tuple[str, ...]) -> list[tuple[str, str]]:
    """返回 [(命中片段, 命中词)]，按片段去重保持顺序。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for segment in segments:
        token = next((item for item in tokens if item in segment), "")
        if token and segment not in seen:
            seen.add(segment)
            found.append((segment, token))
    return found


def _entry(rule_type: str, applicable: bool, rule: str, basis: str, source: str) -> dict[str, Any]:
    return {
        "type": rule_type,
        "applicable": bool(applicable),
        "rule": rule if applicable else "",
        "basis": basis,
        "source": source if applicable else "none",
    }


def build_negative_rule_checklist(
    job: dict[str, Any],
    *,
    restricted_info: dict[str, Any] | None = None,
    archetype: dict[str, Any] | None = None,
    consultant_answers: str = "",
    kb_dir: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """五类排除规则检查清单：逐类输出 {type, applicable, rule, basis, source}。

    参数（全部由运行时传入，本模块不触碰 DB 与 restricted 文件）：
    - job：岗位事实（标题/硬性要求/排除项/学历等字段）；
    - restricted_info：knowledge_base.load_restricted_constraints 的白名单输出
      （禁挖名单 banned_companies/banned_rule 与竞业类键，按客户持久化 → 同客户新岗位自动继承）；
    - archetype：命中的岗位原型（其 negative_rules 作为 KB 规则依据）；
    - consultant_answers：顾问当场补充（禁挖/竞业/背景限制必答）。

    返回 (checklist 五项, trace)。typology 以 KB kb_seed_*.json 为准，缺失按 PRD §4 降级。
    """
    typology, trace = load_negative_rule_typology(kb_dir)
    typology_examples = {entry["type"]: entry.get("example") or "" for entry in typology}

    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    job_segments = _segments(
        "\n".join(
            [
                *[str(job.get(field) or "") for field in _JOB_TEXT_FIELDS],
                *[str(profile.get(field) or "") for field in _PROFILE_TEXT_FIELDS],
            ]
        )
    )
    consultant_segments = _segments(consultant_answers)
    archetype_rules = [str(rule).strip() for rule in (archetype or {}).get("negative_rules") or [] if str(rule or "").strip()]

    constraints = (restricted_info or {}).get("constraints") if isinstance((restricted_info or {}).get("constraints"), dict) else {}
    restricted_source = str((restricted_info or {}).get("source_file") or "")

    checklist: list[dict[str, Any]] = []

    # 1. 在职保护名单（客户级禁挖名单合并；restricted 层按客户读取，新岗位同客户自动继承）
    banned = [str(item).strip() for item in constraints.get("banned_companies") or [] if str(item or "").strip()]
    if banned:
        banned_rule = str(constraints.get("banned_rule") or "").strip()
        text = f"禁挖名单（在职保护）：{'、'.join(banned)}"
        if banned_rule:
            text += f"（{banned_rule}）"
        checklist.append(
            _entry(
                "在职保护名单", True, text,
                f"客户级禁挖名单 {len(banned)} 家来自 restricted 层"
                + (f"（{restricted_source}）" if restricted_source else "")
                + "，按客户持久化，同客户新岗位自动继承",
                "restricted_client",
            )
        )
    else:
        consultant_hit = _hits(consultant_segments, ("禁挖", "在职保护", "不能挖", "不能碰"))
        if consultant_hit:
            text = "；".join(segment for segment, _ in consultant_hit[:2])
            checklist.append(
                _entry("在职保护名单", True, text, "顾问当场补充禁挖/在职保护口径（未入 restricted 层持久化）", "consultant")
            )
        else:
            checklist.append(
                _entry("在职保护名单", False, "", "restricted 层无该客户禁挖名单，顾问亦未补充，按不适用处理", "none")
            )

    # 2-5. 文本依据类：岗位硬性要求 > 顾问补充 > KB 原型规则
    def text_class(
        rule_type: str,
        tokens: tuple[str, ...],
        basis_label: str,
    ) -> dict[str, Any]:
        job_hit = _hits(job_segments, tokens)
        if job_hit:
            rule = "；".join(segment for segment, _ in job_hit[:2])
            return _entry(
                rule_type, True, rule,
                f"岗位硬性要求/JD 含{basis_label}依据（命中“{job_hit[0][1]}”）",
                "jd",
            )
        consultant_hit = _hits(consultant_segments, tokens)
        if consultant_hit:
            rule = "；".join(segment for segment, _ in consultant_hit[:2])
            return _entry(
                rule_type, True, rule,
                f"顾问补充含{basis_label}依据（命中“{consultant_hit[0][1]}”）",
                "consultant",
            )
        kb_hit = _hits(archetype_rules, tokens)
        if kb_hit:
            rule = "；".join(segment for segment, _ in kb_hit[:2])
            return _entry(
                rule_type, True, rule,
                f"KB 岗位原型规则含{basis_label}依据（命中“{kb_hit[0][1]}”）",
                "kb_profile",
            )
        example = typology_examples.get(rule_type) or ""
        basis = f"岗位硬性要求、顾问补充与 KB 原型规则均无{basis_label}依据，按不适用处理"
        if example:
            basis += f"（KB typology 该类示例：{example[:40]}）"
        return _entry(rule_type, False, "", basis, "none")

    checklist.append(text_class("学历门槛", _EDUCATION_TOKENS, "学历门槛"))
    checklist.append(text_class("身份/背景限制", _IDENTITY_TOKENS, "身份/背景限制"))

    # 4. 竞业协议排除：restricted 层竞业键优先，其次文本依据
    non_compete_items = [
        (str(key), value)
        for key, value in constraints.items()
        if key not in ("banned_companies", "banned_rule") and _NON_COMPETE_KEY_PATTERN.search(str(key)) and value not in (None, "", [])
    ]
    if non_compete_items:
        parts = []
        for key, value in non_compete_items:
            if isinstance(value, list):
                value = "、".join(str(item) for item in value if str(item or "").strip())
            text = str(value or "").strip()
            if text:
                parts.append(text)
        if parts:
            checklist.append(
                _entry(
                    "竞业协议排除", True, "；".join(parts[:2]),
                    "客户级竞业约束来自 restricted 层"
                    + (f"（{restricted_source}）" if restricted_source else "")
                    + "，按客户持久化，同客户新岗位自动继承",
                    "restricted_client",
                )
            )
        else:
            checklist.append(text_class("竞业协议排除", _NON_COMPETE_TOKENS, "竞业协议排除"))
    else:
        checklist.append(text_class("竞业协议排除", _NON_COMPETE_TOKENS, "竞业协议排除"))

    # 5. 稳定性筛选（如"五年三跳"）
    checklist.append(text_class("稳定性筛选", _STABILITY_TOKENS, "稳定性筛选"))

    # 固定五类顺序（与 PRD §4 一致），typology 多出类已在 load 留痕，不进清单
    order = {name: index for index, name in enumerate(NEGATIVE_RULE_TYPES)}
    checklist.sort(key=lambda entry: order.get(entry["type"], len(order)))
    for entry in checklist:
        state = "适用" if entry["applicable"] else "不适用"
        trace.append(f"五类清单[{entry['type']}]：{state}（{entry['basis']}）")
    return checklist, trace
