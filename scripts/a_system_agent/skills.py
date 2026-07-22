from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    version: str
    risk_level: str
    supported_contexts: tuple[str, ...]
    input_schema: dict[str, str]
    output_schema: dict[str, str]
    handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    business_stage: str = "cross_stage"
    adapter_type: str = "native"
    timeout_seconds: int = 60
    retry_limit: int = 1
    idempotent: bool = True
    required_permissions: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    rollback_policy: str = "none"
    label: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "risk_level": self.risk_level,
            "supported_contexts": list(self.supported_contexts),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "business_stage": self.business_stage,
            "adapter_type": self.adapter_type,
            "timeout_seconds": self.timeout_seconds,
            "retry_limit": self.retry_limit,
            "idempotent": self.idempotent,
            "required_permissions": list(self.required_permissions),
            "artifact_types": list(self.artifact_types),
            "rollback_policy": self.rollback_policy,
            "label": self.label or self.id,
        }


class NativeAdapter:
    id = "native"

    def execute(self, spec: CapabilitySpec, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return spec.handler(context, inputs)


class ScriptAdapter(NativeAdapter):
    id = "script"

    def execute(self, spec: CapabilitySpec, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        result = super().execute(spec, context, inputs)
        if spec.artifact_types and result.get("blocked") is not True and not isinstance(result.get("artifacts"), list):
            raise ValueError(f"脚本能力 {spec.id} 必须返回可审计产物")
        return result


class BrowserAdapter(NativeAdapter):
    id = "browser"

    def execute(self, spec: CapabilitySpec, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        if spec.risk_level in {"R2", "R3"} and inputs.get("_approval_granted") is not True:
            raise ValueError(f"浏览器能力 {spec.id} 缺少单次审批授权")
        return super().execute(spec, context, inputs)


ADAPTERS = {adapter.id: adapter for adapter in (NativeAdapter(), ScriptAdapter(), BrowserAdapter())}


class CapabilityRegistry:
    def __init__(self, enabled: list[str] | None = None) -> None:
        self._skills: dict[str, CapabilitySpec] = {}
        self._enabled = set(enabled or [])

    def register(self, spec: CapabilitySpec) -> None:
        if spec.id in self._skills:
            raise ValueError(f"Skill 已注册：{spec.id}")
        self._skills[spec.id] = spec

    def list(self) -> list[dict[str, Any]]:
        return [
            {**spec.public(), "enabled": not self._enabled or spec.id in self._enabled}
            for spec in self._skills.values()
        ]

    def get(self, skill_id: str) -> CapabilitySpec | None:
        spec = self._skills.get(str(skill_id or ""))
        if spec is None or (self._enabled and spec.id not in self._enabled):
            return None
        return spec

    def execute(self, skill_id: str, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(skill_id)
        if spec is None:
            raise ValueError(f"未注册或未启用的 Skill：{skill_id}")
        context_type = str(context.get("type") or "global")
        if context_type not in spec.supported_contexts:
            raise ValueError(f"Skill {spec.id} 不支持 {context_type} 上下文")
        if not isinstance(inputs, dict):
            raise ValueError("Skill inputs 必须是对象")
        for key, kind in spec.input_schema.items():
            if key.endswith("?"):
                continue
            if key not in inputs:
                raise ValueError(f"Skill 缺少输入：{key}")
            if kind == "integer" and not isinstance(inputs[key], int):
                raise ValueError(f"Skill 输入 {key} 必须是整数")
            if kind == "string" and not isinstance(inputs[key], str):
                raise ValueError(f"Skill 输入 {key} 必须是字符串")
            if kind == "object" and not isinstance(inputs[key], dict):
                raise ValueError(f"Skill 输入 {key} 必须是对象")
        if spec.risk_level in {"R2", "R3"} and inputs.get("_approval_granted") is not True:
            raise ValueError(f"能力 {spec.id} 必须经过单次审批")
        adapter = ADAPTERS.get(spec.adapter_type)
        if adapter is None:
            raise ValueError(f"能力 {spec.id} 使用未知 Adapter：{spec.adapter_type}")
        result = adapter.execute(spec, context, inputs)
        if not isinstance(result, dict):
            raise ValueError(f"Skill {spec.id} 返回值必须是对象")
        return {"skill": spec.public(), "result": result}


# Backward compatibility for the v1.7 API and existing tests.
SkillSpec = CapabilitySpec
SkillRegistry = CapabilityRegistry
