"""
Config/Profile loading utilities.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import yaml  # type: ignore

from .models import Profile


class Config:
    def __init__(self, data: Dict[str, Any]):
        self.mode = data.get("mode", "review")
        self.max_jobs_per_run = int(data.get("max_jobs_per_run", 10))
        self.log_dir = data.get("log_dir", "out/logs")
        self.llm_model = data.get("llm_model", "gpt-4o-mini")
        self.llm_temperature = float(data.get("llm_temperature", 0.2))
        self.prefer_not_to_say = bool(data.get("prefer_not_to_say", True))
        self.screenshot = bool(data.get("screenshot", True))
        # Gemini config
        self.gemini_api_key = data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        self.gemini_model = data.get("gemini_model", "models/gemini-2.5-flash")
        # Browser
        self.headless = bool(data.get("headless", False))
        # Email for verification codes
        self.email_user = data.get("email_user", "")
        self.email_app_password = data.get("email_app_password", "")


def load_profile(path: str | Path) -> Profile:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback to YAML
        data = yaml.safe_load(text)
    return Profile(**data)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if text.strip() else {}
    return Config(data or {})
