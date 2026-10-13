"""System prompt template renderer for the PMT assistant.
Fills workspace, user, date, and locale context variables on each turn.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SPEC_DIR = Path(__file__).resolve().parent / "spec"
if not SPEC_DIR.exists():
    SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"

PROMPT_TEMPLATE_PATH = SPEC_DIR / "system_prompt.md"


class PromptBuilder:
    def __init__(self) -> None:
        if PROMPT_TEMPLATE_PATH.exists():
            with open(PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                self._template = f.read()
        else:
            self._template = "You are the assistant inside Project Management Tool (PMT)."

    def render(
        self,
        workspace_name: str,
        user_name: str,
        user_id: int | str,
        user_role: str,
        today: Optional[str] = None,
        weekday: Optional[str] = None,
        tz: str = "UTC",
        date_format: str = "DD-MM-YYYY",
        language: str = "English",
    ) -> str:
        now = datetime.now(timezone.utc)
        iso_today = today or now.strftime("%Y-%m-%d")
        day_name = weekday or now.strftime("%A")

        rendered = self._template
        rendered = rendered.replace("{{workspace_name}}", str(workspace_name))
        rendered = rendered.replace("{{user_name}}", str(user_name))
        rendered = rendered.replace("{{user_id}}", str(user_id))
        rendered = rendered.replace("{{user_role}}", str(user_role))
        rendered = rendered.replace("{{today}}", iso_today)
        rendered = rendered.replace("{{weekday}}", day_name)
        rendered = rendered.replace("{{timezone}}", tz)
        rendered = rendered.replace("{{date_format}}", date_format)
        rendered = rendered.replace("{{language}}", language)

        return rendered

    def build_system_prompt(self, context_vars: dict[str, Any]) -> str:
        return self.render(
            workspace_name=context_vars.get("workspace_name", "Workspace"),
            user_name=context_vars.get("user_name", "User"),
            user_id=context_vars.get("user_id", "1"),
            user_role=context_vars.get("user_role", "member"),
            today=context_vars.get("today"),
            weekday=context_vars.get("weekday"),
            tz=context_vars.get("timezone", "UTC"),
            date_format=context_vars.get("date_format", "DD-MM-YYYY"),
            language=context_vars.get("language", "English"),
        )


prompt_builder = PromptBuilder()

