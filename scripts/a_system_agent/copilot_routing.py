"""Facade for Copilot routing (P2-1 split).

Implementation has been split into two domain modules:
- copilot_impl.py         — structured-action understanding, receipts, model answer, main _copilot_impl
- copilot_skill_routes.py — sourcing strategy gate, client mention routing, skill route table

This module only re-exports the split names, so existing imports
(copilot_handler.py, tests) keep working unchanged with identical objects.
"""

from .copilot_impl import (  # noqa: F401 模块级兼容 re-export（既有测试/调用方不变）
    _STRUCTURED_ACTION_SEMANTICS,
    _STRUCTURED_ACTION_COMMANDS,
    _structured_action_command,
    _candidate_intent_understanding,
    _correction_understanding,
    _structured_action_understanding,
    _understanding_card,
    _normalize_action_suggestions,
    _execution_receipt,
    _generate_copilot_model_answer,
    _copilot_impl,
)
from .copilot_skill_routes import (  # noqa: F401 模块级兼容 re-export（既有测试/调用方不变）
    _sourcing_strategy_gate,
    _mentioned_client_names,
    _route_copilot_skills,
)
