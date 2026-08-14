from __future__ import annotations

import os  # noqa: F401 兼容模块属性：tests patch a_system_agent.workflow.os.getpid
from typing import Any

from .workflow_execute import WorkflowExecuteMixin
from .workflow_plan import (  # noqa: F401 模块级兼容 re-export（既有调用方/测试不变）
    BUSINESS_OUTCOME_LABELS,
    BUSINESS_OUTCOMES,
    SOURCING_OBJECTIVE_TOKENS,
    STAGE_ORDER,
    WorkflowPlanMixin,
    _dumps,
    _loads,
    _mask_candidate_name,
    _row,
    classify_business_outcome,
    sourcing_target_stats,
)
from .workflow_review import WorkflowReviewMixin


class WorkflowEngine(WorkflowPlanMixin, WorkflowExecuteMixin, WorkflowReviewMixin):
    """ASA 目标工作流引擎。

    Mixin 组合 facade（P2-1）：规划阶段在 WorkflowPlanMixin
    （workflow_plan.py），执行阶段在 WorkflowExecuteMixin
    （workflow_execute.py），读取/回顾阶段在 WorkflowReviewMixin
    （workflow_review.py）。方法体逐字节迁移，语义不变；模块级名字全部
    re-export，既有 a_system_agent.workflow 调用方拿到的对象保持同一。
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self._recover_interrupted()
        self._refresh_goal_titles()

    def _connect(self):
        return self.service._connect()

