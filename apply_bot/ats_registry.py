"""
ATS flow registry and lightweight adapters.

Each flow exposes:
- extract_questions(page) -> list[QuestionBlock]
- page_is_confirmation(page) -> bool
- fill_and_next(page, answers) -> None (fills current page and advances)

The registry keeps branching logic isolated from the core NavigationController.
"""

from __future__ import annotations

from typing import Dict

from .flows import (
    GenericFlow,
    LeverFlow,
    GreenhouseFlow,
    WorkdayFlow,
    AshbyFlow,
    SmartRecruitersFlow,
    BaseFlow,
)

ATS_FLOW_REGISTRY: Dict[str, BaseFlow] = {
    "lever": LeverFlow(),
    "greenhouse": GreenhouseFlow(),
    "workday": WorkdayFlow(),
    "ashby": AshbyFlow(),
    "smartrecruiters": SmartRecruitersFlow(),
    "default": GenericFlow(),
}


def get_flow(ats: str) -> BaseFlow:
    key = (ats or "").lower().strip()
    return ATS_FLOW_REGISTRY.get(key, ATS_FLOW_REGISTRY["default"])
