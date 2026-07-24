"""S5-3：知识回流 —— Mapping 任务卡团队数据回流公司图谱 teams 扩展层（知识库维护流程）。

口径来源（事实源）：PRD docs/ASA_PRD_S5_mapping_direct_sourcing_2026-07-23.md §5（知识回流）/§7（硬性约束）。

红线（写死，违反即返工）：
- 图谱 JSON 的写入只经本模块的知识库维护流程（显式触发 + as_of 日期）；
  运行时读取路径（knowledge_base.load_company_graph）保持只读，Core 绝不自动写图谱。
- 只回流"确认过"的团队结构：团队下存在状态 ∈ CONFIRMED_PLUS（confirmed/contacted/replied/intaken）
  候选的团队才入图谱；只写公司/团队名/人数提示/关键角色/as_of/来源 artifact——
  候选人名（无论遮罩与否）、费率、话术红线、restricted 层任何内容永远不进图谱。
- 禁挖名单回流前再校验一次（restricted 仅 banned_companies 白名单出库）；禁挖公司整队跳过并计数。
- 原 589 家底图不污染：命中图谱的公司在该条目下加 teams 键；未命中的公司进顶层 teams_external 区。
- 字节保留：除 teams/teams_external 相关键外原文件逐字节保留——实现为
  json.loads 原文件后按原序列化口径（ensure_ascii=False, indent=1，无尾换行）确定性重写，
  已验证该口径下未修改的解析结果与原文件逐字节一致，故 diff 只含 teams 区。

幂等（PRD §5：同 artifact 重复回流更新 as_of 不重复条目）：
- 条目身份 = (公司, 规范化团队名)；同 artifact 重复回流只更新 as_of，不新增条目；
- 跨 artifact 同一 (公司,团队) 合并为单条：key_roles 并集（上限 MAX_KEY_ROLES）、
  headcount_hint 取 max（它是确认线索数提示，跨岗位不可加和）、as_of/source_artifact 取最新；
- 序列化结果与原文件逐字节一致时不写盘（summary.changed=False）。

teams 条目 schema（公司条目 companies[name].teams[] 与顶层 teams_external[name] 同构）：
- name            团队名（mapping_task target_teams.team 原文）
- headcount_hint  人数提示 = 该团队已确认及以上状态的候选数（是确认线索数，不是真实编制）
- key_roles       关键角色 = 这些候选的 current_role 去重（保序，上限 MAX_KEY_ROLES）
- as_of           回流日期（YYYY-MM-DD）
- source_artifact 来源 mapping_task artifact_id（可回溯）

CLI（主控复核后对真实 artifact 执行；默认干跑，--write 才落盘）：
    PYTHONPATH=scripts /usr/local/bin/python3 -m a_system_agent.graph_teams_backflow \
        --db <生产库路径> --artifact-id mapping_task_workflow_15fc23c21ce8 \
        --kb-dir /Users/messi/Documents/ASA/knowledge_base --write
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import knowledge_base

# 已确认及以上状态（与 mapping_task._sync_stats 的 confirmed 口径一致：曾被确认即计入）
CONFIRMED_PLUS = ("confirmed", "contacted", "replied", "intaken")

MAX_KEY_ROLES = 6

# 图谱 JSON 序列化口径（与原文件逐字节一致的前提；改动即破坏字节保留）
_GRAPH_DUMP_KWARGS = {"ensure_ascii": False, "indent": 1}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _norm_team_name(value: Any) -> str:
    """团队名归一（条目身份用）：去全部空白 + 小写。"""
    return "".join(str(value or "").split()).lower()


def _match_graph_company(companies: dict[str, Any], company: str) -> str | None:
    """图谱公司精确/别名命中（复用 knowledge_base 规范化口径，宁可 miss 进 teams_external 不可错配）。"""
    target_raw = " ".join(str(company or "").split())
    target_norm = knowledge_base.normalize_client_name(company)
    if not target_norm:
        return None
    for name in companies:
        rule, _reason = knowledge_base.name_match_rule(target_raw, target_norm, name)
        if rule:
            return str(name)
    return None


def collect_confirmed_teams(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """从 mapping_v1 文档抽取"已确认"团队数据（纯函数，不碰 DB/文件）。

    返回 [{company, team, headcount_hint, key_roles}]；无确认候选的团队不出现。
    候选人名/备注/来源 URL 一律不抽取（红线：名单不进图谱）。
    """
    doc = doc if isinstance(doc, dict) else {}
    teams = doc.get("target_teams") or []
    candidates = doc.get("candidates") or []
    per_team: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("status") or "") not in CONFIRMED_PLUS:
            continue
        team_ref = candidate.get("team_ref")
        if not isinstance(team_ref, int) or not 0 <= team_ref < len(teams):
            continue
        bucket = per_team.setdefault(team_ref, {"count": 0, "roles": []})
        bucket["count"] += 1
        role = " ".join(str(candidate.get("current_role") or "").split())
        if role and role not in bucket["roles"] and len(bucket["roles"]) < MAX_KEY_ROLES:
            bucket["roles"].append(role)
    collected: list[dict[str, Any]] = []
    for team_ref in sorted(per_team):
        team = teams[team_ref] if isinstance(teams[team_ref], dict) else {}
        company = " ".join(str(team.get("company") or "").split())
        team_name = " ".join(str(team.get("team") or "").split())
        if not company or not team_name:
            continue
        collected.append(
            {
                "company": company,
                "team": team_name,
                "headcount_hint": int(per_team[team_ref]["count"]),
                "key_roles": list(per_team[team_ref]["roles"]),
            }
        )
    return collected


def _upsert_team_entry(
    teams_list: list[Any],
    *,
    name: str,
    headcount_hint: int,
    key_roles: list[str],
    as_of: str,
    source_artifact: str,
) -> str:
    """按 (规范化团队名) 幂等 upsert；返回 'inserted' / 'updated'（merged）。同值合并返回 'unchanged'。"""
    norm = _norm_team_name(name)
    for existing in teams_list:
        if not isinstance(existing, dict) or _norm_team_name(existing.get("name")) != norm:
            continue
        before = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        roles = [str(role) for role in existing.get("key_roles") or [] if str(role or "").strip()]
        for role in key_roles:
            if role not in roles and len(roles) < MAX_KEY_ROLES:
                roles.append(role)
        existing["key_roles"] = roles
        existing["headcount_hint"] = max(int(existing.get("headcount_hint") or 0), int(headcount_hint))
        existing["as_of"] = as_of
        existing["source_artifact"] = str(source_artifact)
        after = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        return "updated" if after != before else "unchanged"
    teams_list.append(
        {
            "name": name,
            "headcount_hint": int(headcount_hint),
            "key_roles": list(key_roles),
            "as_of": as_of,
            "source_artifact": str(source_artifact),
        }
    )
    return "inserted"


def backflow_teams(
    graph_path: str | Path,
    doc: dict[str, Any],
    *,
    artifact_id: str,
    as_of: str = "",
    banned: list[str] | tuple[str, ...] = (),
    write: bool = True,
) -> dict[str, Any]:
    """把 mapping_task 已确认团队数据回流进图谱 JSON 的 teams 扩展层。

    graph_path 不存在/坏 JSON/结构异常 → ValueError；无已确认团队可回流 → ValueError（不空写）。
    write=False 为干跑（只算摘要不落盘，供复核）。返回写入摘要（公司数/团队数/as_of 等）。
    """
    as_of = as_of or _today()
    path = Path(graph_path)
    if not path.is_file():
        raise ValueError(f"公司图谱缺失：{path}")
    try:
        original_text = path.read_text(encoding="utf-8")
        graph_doc = json.loads(original_text)
    except (OSError, ValueError) as exc:
        raise ValueError(f"公司图谱解析失败（{exc.__class__.__name__}）：{path}") from exc
    companies = graph_doc.get("companies") if isinstance(graph_doc, dict) else None
    if not isinstance(companies, dict):
        raise ValueError(f"公司图谱结构异常（缺 companies 对象）：{path}")

    collected = collect_confirmed_teams(doc)
    if not collected:
        raise ValueError("任务卡没有已确认（confirmed 及以上）的团队数据可回流")

    banned = [str(item) for item in banned if str(item or "").strip()]
    external = graph_doc.get("teams_external")
    if external is None:
        external = {}
    if not isinstance(external, dict):
        raise ValueError("公司图谱 teams_external 区结构异常（必须是对象），拒绝覆盖")

    summary = {
        "ok": True,
        "artifact_id": str(artifact_id),
        "as_of": as_of,
        "graph_path": str(path),
        "companies_written": 0,
        "teams_written": 0,
        "teams_inserted": 0,
        "teams_updated": 0,
        "external_companies_written": 0,
        "skipped_banned": 0,
        "written_companies": [],
        "external_companies": [],
        "changed": False,
        "dry_run": not write,
    }
    graph_companies_touched: set[str] = set()
    external_companies_touched: set[str] = set()
    for item in collected:
        company = item["company"]
        target_raw = " ".join(company.split())
        target_norm = knowledge_base.normalize_client_name(company)
        if any(
            knowledge_base.name_match_rule(target_raw, target_norm, banned_name)[0]
            for banned_name in banned
        ):
            summary["skipped_banned"] += 1
            continue  # 红线：禁挖公司整队跳过
        hit = _match_graph_company(companies, company)
        if hit is not None:
            entry = companies.get(hit)
            if not isinstance(entry, dict):
                raise ValueError(f"图谱公司条目结构异常：{hit}")
            teams_list = entry.setdefault("teams", [])
            if not isinstance(teams_list, list):
                raise ValueError(f"图谱公司 {hit} 的 teams 键结构异常（必须是数组），拒绝覆盖")
            graph_companies_touched.add(hit)
        else:
            teams_list = external.setdefault(company, [])
            if not isinstance(teams_list, list):
                raise ValueError(f"teams_external[{company}] 结构异常（必须是数组），拒绝覆盖")
            external_companies_touched.add(company)
        outcome = _upsert_team_entry(
            teams_list,
            name=item["team"],
            headcount_hint=item["headcount_hint"],
            key_roles=item["key_roles"],
            as_of=as_of,
            source_artifact=str(artifact_id),
        )
        if outcome == "inserted":
            summary["teams_inserted"] += 1
        elif outcome == "updated":
            summary["teams_updated"] += 1
    # external 与既有 graph_doc["teams_external"] 是同一引用；仅新增且有内容时才挂键
    # （既有空区原样保留，不增删键，保字节保留口径）
    if external and "teams_external" not in graph_doc:
        graph_doc["teams_external"] = external

    summary["companies_written"] = len(graph_companies_touched)
    summary["external_companies_written"] = len(external_companies_touched)
    summary["teams_written"] = summary["teams_inserted"] + summary["teams_updated"]
    summary["written_companies"] = sorted(graph_companies_touched)
    summary["external_companies"] = sorted(external_companies_touched)
    if summary["skipped_banned"] == len(collected):
        raise ValueError(f"可回流团队全部在禁挖名单内（{summary['skipped_banned']} 个），拒绝写入")

    serialized = json.dumps(graph_doc, **_GRAPH_DUMP_KWARGS)
    summary["changed"] = serialized != original_text
    if write and summary["changed"]:
        # 原子写：同目录临时文件 + os.replace，防半写状态
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(serialized)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return summary


# ---------------------------------------------------------------------------
# CLI（知识库维护流程入口：默认干跑，--write 才落盘；绝不触碰生产 DB 的写路径）
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S5-3 Mapping 任务卡团队数据回流公司图谱（teams 扩展层）")
    parser.add_argument("--db", default=os.environ.get("A_SYSTEM_DB", ""), help="ASA 生产库路径（只读使用）")
    parser.add_argument("--artifact-id", required=True, help="mapping_task artifact_id")
    parser.add_argument("--kb-dir", default="", help="知识库目录（缺省走 ASA_KNOWLEDGE_BASE_DIR/默认目录）")
    parser.add_argument("--as-of", default="", help="回流日期（YYYY-MM-DD，缺省今天）")
    parser.add_argument("--write", action="store_true", help="实际写盘（缺省干跑只打印摘要）")
    args = parser.parse_args(argv)

    if not args.db:
        print("错误：--db 必填（或设 A_SYSTEM_DB）", file=sys.stderr)
        return 2
    from . import mapping_task
    from .strategy_v2 import knowledge_base_dir

    kb_dir = Path(args.kb_dir).expanduser() if args.kb_dir else knowledge_base_dir()
    conn = sqlite3.connect(str(Path(args.db).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        payload = mapping_task.get_mapping_task(conn, str(args.artifact_id))
    finally:
        conn.close()
    if payload is None:
        print(f"错误：Mapping 任务卡不存在：{args.artifact_id}", file=sys.stderr)
        return 2
    doc = payload["mapping_task"]
    restricted, _trace = knowledge_base.load_restricted_constraints(str(doc.get("client") or ""), kb_dir=kb_dir)
    constraints = (restricted or {}).get("constraints") if isinstance(restricted, dict) else {}
    banned = [str(item) for item in (constraints or {}).get("banned_companies") or [] if str(item or "").strip()]
    try:
        summary = backflow_teams(
            kb_dir / knowledge_base.COMPANY_GRAPH_FILE,
            doc,
            artifact_id=str(args.artifact_id),
            as_of=str(args.as_of or ""),
            banned=banned,
            write=bool(args.write),
        )
    except ValueError as exc:
        print(f"拒绝回流：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.write:
        print("\n（干跑，未落盘；确认无误后加 --write 执行）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
