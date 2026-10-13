"""Policy Engine for enforcing Tiers T0-T3, Confirmation Cards, Typed Confirmation,
Idempotency Keys, and Audit Logging.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from ai_chat.registry import ToolDefinition
from models.activity_log import ActivityLog


@dataclass
class ConfirmCard:
    confirm_id: str
    tool_name: str
    tier: str  # T2 or T3
    summary: str
    resolved_args: dict[str, Any]
    preview_data: dict[str, Any] = field(default_factory=dict)
    requires_typed_confirmation: bool = False
    expected_typed_text: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class UndoAction:
    action_id: str
    tool_name: str
    reverse_tool: str
    reverse_args: dict[str, Any]
    description: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 600)  # 10 mins


class PolicyEngine:
    def __init__(self) -> None:
        # In-memory storage for pending confirmations & undo actions
        self._pending_confirmations: dict[str, ConfirmCard] = {}
        self._pending_undos: dict[str, UndoAction] = {}
        self._idempotency_cache: dict[str, Any] = {}

    def compute_idempotency_key(
        self,
        conversation_id: str,
        turn_index: int,
        tool_name: str,
        resolved_args: dict[str, Any],
    ) -> str:
        clean_args = {k: v for k, v in resolved_args.items() if not k.startswith("_")}
        payload = f"{conversation_id}:{turn_index}:{tool_name}:{json.dumps(clean_args, sort_keys=True)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def check_idempotent_result(self, idempotency_key: str) -> Optional[Any]:
        return self._idempotency_cache.get(idempotency_key)

    def record_idempotent_result(self, idempotency_key: str, result: Any) -> None:
        self._idempotency_cache[idempotency_key] = result

    def create_confirm_card(
        self,
        tool: ToolDefinition,
        resolved_args: dict[str, Any],
        preview_data: dict[str, Any],
        summary: str,
        entity_key_or_name: Optional[str] = None,
    ) -> ConfirmCard:
        confirm_id = f"conf_{uuid.uuid4().hex[:12]}"
        is_typed = tool.policy.tier == "T3" or tool.policy.confirm == "typed"

        card = ConfirmCard(
            confirm_id=confirm_id,
            tool_name=tool.name,
            tier=tool.policy.tier,
            summary=summary,
            resolved_args=resolved_args,
            preview_data=preview_data,
            requires_typed_confirmation=is_typed,
            expected_typed_text=entity_key_or_name if is_typed else None,
        )
        self._pending_confirmations[confirm_id] = card
        return card

    def validate_confirmation(
        self,
        confirm_id: str,
        typed_text: Optional[str] = None,
    ) -> tuple[bool, Optional[ConfirmCard], Optional[str]]:
        card = self._pending_confirmations.get(confirm_id)
        if not card:
            return False, None, "Confirmation token is invalid or expired."

        if card.requires_typed_confirmation:
            if not typed_text or typed_text.strip().lower() != (card.expected_typed_text or "").strip().lower():
                return (
                    False,
                    card,
                    f"Typed confirmation does not match. Please type '{card.expected_typed_text}' to confirm.",
                )

        # Pop from pending once validated
        del self._pending_confirmations[confirm_id]
        return True, card, None

    def register_undo(
        self,
        tool_name: str,
        reverse_tool: str,
        reverse_args: dict[str, Any],
        description: str,
    ) -> UndoAction:
        action_id = f"undo_{uuid.uuid4().hex[:12]}"
        action = UndoAction(
            action_id=action_id,
            tool_name=tool_name,
            reverse_tool=reverse_tool,
            reverse_args=reverse_args,
            description=description,
        )
        self._pending_undos[action_id] = action
        return action

    def get_undo(self, action_id: str) -> Optional[UndoAction]:
        action = self._pending_undos.get(action_id)
        if action and action.expires_at > time.time():
            return action
        return None

    def log_audit(
        self,
        db: Session,
        user_id: int,
        workspace_id: str,
        tool_name: str,
        resolved_args: dict[str, Any],
        outcome: str = "success",
    ) -> None:
        """Records an audit log entry in the database for the executed write."""
        try:
            clean_args = {k: v for k, v in resolved_args.items() if not k.startswith("_")}
            log_entry = ActivityLog(
                user_id=user_id,
                action=f"chat_tool:{tool_name}",
                details=json.dumps({
                    "workspace_id": workspace_id,
                    "tool": tool_name,
                    "args": clean_args,
                    "outcome": outcome,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }),
            )
            db.add(log_entry)
            db.commit()
        except Exception:
            db.rollback()


# Global policy engine instance
policy_engine = PolicyEngine()
