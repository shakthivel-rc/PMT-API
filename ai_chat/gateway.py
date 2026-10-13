"""The central Chat Gateway orchestrating context, routing, LLM adapter,
resolving references, enforcing RBAC and T0-T3 policy tiers, and executing handlers.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ai_chat.arg_resolver import resolve_tool_arguments
from ai_chat.domain_router import domain_router
from ai_chat.handlers import HANDLER_MAP
from ai_chat.handlers.context import ToolContext
from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall
from ai_chat.llm.settings import resolve_adapter_for_user
from ai_chat.policy_engine import ConfirmCard, UndoAction, policy_engine
from ai_chat.prompt_builder import prompt_builder
from ai_chat.rbac import check_tool_permission
from ai_chat.registry import TOOL_REGISTRY, ToolDefinition
from dependencies.workspace import WorkspaceAccess
from models.chat_session import ChatConversation, ChatMessageRecord
from models.user import User
from models.workspace import Workspace


@dataclass
class ChatTurnResult:
    conversation_id: str
    reply: Optional[str] = None
    tool_calls_executed: list[dict[str, Any]] = field(default_factory=list)
    confirm_card: Optional[dict[str, Any]] = None
    undo_action: Optional[dict[str, Any]] = None
    clarification: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class ChatGateway:
    def __init__(self) -> None:
        self.router = domain_router
        self.registry = TOOL_REGISTRY
        self.policy = policy_engine

    def execute_turn(
        self,
        db: Session,
        access: WorkspaceAccess,
        current_user: User,
        message: Optional[str] = None,
        conversation_id: Optional[str] = None,
        confirm_action_id: Optional[str] = None,
        confirm_typed_text: Optional[str] = None,
        cancel_action_id: Optional[str] = None,
        undo_action_id: Optional[str] = None,
        adapter: Optional[BaseLLMAdapter] = None,
        context_vars: Optional[dict[str, str]] = None,
    ) -> ChatTurnResult:
        workspace_id = access.workspace_id
        user_id = access.user_id
        user_role = access.role

        # Build execution context
        ctx = ToolContext(
            db=db,
            workspace_id=workspace_id,
            current_user=current_user,
            access=access,
            role=user_role,
        )

        # -------------------------------------------------------------
        # 1. Handle Undo Action
        # -------------------------------------------------------------
        if undo_action_id:
            undo = self.policy.get_undo(undo_action_id)
            if not undo:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error="Undo action has expired or does not exist.",
                )
            reverse_handler = HANDLER_MAP.get(undo.reverse_tool)
            if not reverse_handler:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=f"No handler found for reverse action {undo.reverse_tool}.",
                )
            try:
                reverse_handler(ctx, undo.reverse_args)
                self.policy.log_audit(
                    db=db,
                    user_id=int(user_id) if str(user_id).isdigit() else 0,
                    workspace_id=workspace_id,
                    tool_name=undo.reverse_tool,
                    resolved_args=undo.reverse_args,
                    outcome="success",
                )
                msg_reply = f"Successfully undone: {undo.description}"
                if conversation_id:
                    self._save_message(db, conversation_id, "assistant", msg_reply)
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    reply=msg_reply,
                )
            except Exception as e:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=f"Failed to perform undo: {str(e)}",
                )

        # -------------------------------------------------------------
        # 2. Handle Cancel Confirmation
        # -------------------------------------------------------------
        if cancel_action_id:
            if cancel_action_id in self.policy._pending_confirmations:
                card = self.policy._pending_confirmations.pop(cancel_action_id)
                msg_reply = f"Action cancelled: {card.summary}"
                if conversation_id:
                    self._save_message(db, conversation_id, "assistant", msg_reply)
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    reply=msg_reply,
                )
            return ChatTurnResult(
                conversation_id=conversation_id or "default",
                error="Confirmation token is invalid or already resolved.",
            )

        # -------------------------------------------------------------
        # 3. Handle Confirm Action (T2 / T3)
        # -------------------------------------------------------------
        if confirm_action_id:
            ok, card, err = self.policy.validate_confirmation(
                confirm_id=confirm_action_id,
                typed_text=confirm_typed_text,
            )
            if not ok or not card:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=err or "Invalid confirmation.",
                )

            # Check RBAC
            allowed, rbac_err = check_tool_permission(card.tool_name, user_role)
            if not allowed:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=rbac_err,
                )

            handler = HANDLER_MAP.get(card.tool_name)
            if not handler:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=f"Handler not found for tool {card.tool_name}",
                )

            try:
                exec_result = handler(ctx, card.resolved_args)
                # Audit log
                self.policy.log_audit(
                    db=db,
                    user_id=int(user_id) if str(user_id).isdigit() else 0,
                    workspace_id=workspace_id,
                    tool_name=card.tool_name,
                    resolved_args=card.resolved_args,
                    outcome="success",
                )

                undo_data = None
                tool_def = self.registry.get(card.tool_name)
                if tool_def and tool_def.policy.undo:
                    undo = self.policy.register_undo(
                        tool_name=card.tool_name,
                        reverse_tool=tool_def.policy.undo,
                        reverse_args=card.resolved_args,
                        description=card.summary,
                    )
                    undo_data = asdict(undo)

                msg_reply = f"Confirmed and executed: {card.summary}"
                if conversation_id:
                    self._save_message(
                        db,
                        conversation_id,
                        "assistant",
                        msg_reply,
                        undo_action=undo_data,
                    )

                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    reply=msg_reply,
                    tool_calls_executed=[{
                        "tool": card.tool_name,
                        "args": card.resolved_args,
                        "result": exec_result,
                    }],
                    undo_action=undo_data,
                )
            except Exception as e:
                return ChatTurnResult(
                    conversation_id=conversation_id or "default",
                    error=f"Execution failed: {str(e)}",
                )

        # -------------------------------------------------------------
        # 4. Standard Message Turn
        # -------------------------------------------------------------
        if not message:
            return ChatTurnResult(
                conversation_id=conversation_id or "default",
                error="No message or action provided.",
            )

        # Ensure conversation exists
        conv = self._get_or_create_conversation(db, workspace_id, user_id, conversation_id)
        conversation_id = conv.id

        # Save user message to database history
        self._save_message(db, conversation_id, "user", message)

        # Resolve LLM adapter
        if not adapter:
            try:
                adapter = resolve_adapter_for_user(db, str(user_id))
            except ValueError as ve:
                err_msg = (
                    "⚠️ No LLM API key configured yet. Please click the Settings gear icon "
                    "in the top right to configure your OpenAI, Anthropic, or Google Gemini API key."
                )
                self._save_message(db, conversation_id, "assistant", err_msg)
                return ChatTurnResult(
                    conversation_id=conversation_id,
                    reply=err_msg,
                    error=str(ve),
                )

        # Build context variables for system prompt
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        ws_name = ws.name if ws else "Workspace"
        full_user_name = f"{current_user.first_name} {current_user.last_name}".strip()

        cv = {
            "workspace_name": ws_name,
            "user_name": full_user_name or current_user.username,
            "user_id": str(user_id),
            "user_role": user_role,
        }
        if context_vars:
            cv.update(context_vars)

        system_prompt = prompt_builder.build_system_prompt(cv)

        # Route tools for this turn (core + max 2 domains)
        selected_tool_defs = self.router.route_turn(message)
        tool_schemas = self.registry.get_tools_schema_for_provider("openai", selected_tool_defs)

        # Fetch conversation history
        history_records = (
            db.query(ChatMessageRecord)
            .filter(ChatMessageRecord.conversation_id == conversation_id)
            .order_by(ChatMessageRecord.created_at.asc())
            .all()
        )

        llm_messages: list[LLMMessage] = [
            LLMMessage(role="system", content=system_prompt)
        ]

        for r in history_records:
            tcs = None
            if r.tool_calls:
                tcs = [
                    LLMToolCall(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                    )
                    for tc in r.tool_calls
                ]
            llm_messages.append(
                LLMMessage(
                    role=r.role,
                    content=r.content,
                    name=r.name,
                    tool_call_id=r.tool_call_id,
                    tool_calls=tcs,
                )
            )

        # Turn Loop: execute model -> handle tool calls -> feed back to model (up to 5 turns)
        executed_tools_summary: list[dict[str, Any]] = []
        turn_index = len(history_records)

        for _ in range(5):
            llm_result: LLMResult = adapter.complete(llm_messages, tools=tool_schemas)

            # If no tool calls, model returned its final textual reply
            if not llm_result.tool_calls:
                reply_text = llm_result.content or ""
                self._save_message(db, conversation_id, "assistant", reply_text)
                return ChatTurnResult(
                    conversation_id=conversation_id,
                    reply=reply_text,
                    tool_calls_executed=executed_tools_summary,
                )

            # Process tool calls
            assistant_msg = LLMMessage(
                role="assistant",
                content=llm_result.content,
                tool_calls=llm_result.tool_calls,
            )
            llm_messages.append(assistant_msg)
            self._save_message(
                db,
                conversation_id,
                "assistant",
                llm_result.content,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in llm_result.tool_calls
                ],
            )

            # Process each tool call sequentially
            for tc in llm_result.tool_calls:
                turn_index += 1
                tool_def = self.registry.get(tc.name)
                if not tool_def:
                    err_resp = f"Tool '{tc.name}' is not recognized or not exposed."
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=err_resp))
                    self._save_message(db, conversation_id, "tool", err_resp, tool_call_id=tc.id, name=tc.name)
                    continue

                # 1. Resolve arguments
                resolved_args, resolve_err, candidates = resolve_tool_arguments(
                    db=db,
                    workspace_id=workspace_id,
                    caller_id=str(user_id),
                    tool_name=tc.name,
                    args=tc.arguments,
                )

                if candidates:
                    # Ambiguous entity (e.g. Sarah Khan vs Sarah Lee)
                    cand_names = [c.get("name") for c in candidates if "name" in c]
                    clarify_text = f"Multiple matching candidates found. Did you mean {' or '.join(cand_names)}?"
                    self._save_message(db, conversation_id, "assistant", clarify_text)
                    return ChatTurnResult(
                        conversation_id=conversation_id,
                        reply=clarify_text,
                        clarification={
                            "tool_name": tc.name,
                            "candidates": candidates,
                            "raw_args": tc.arguments,
                        },
                    )

                if resolve_err:
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=f"Resolution error: {resolve_err}"))
                    self._save_message(db, conversation_id, "tool", f"Resolution error: {resolve_err}", tool_call_id=tc.id, name=tc.name)
                    continue

                # 2. Check RBAC permissions
                allowed, rbac_err = check_tool_permission(tc.name, user_role)
                if not allowed:
                    err_msg = rbac_err or "Permission denied."
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=err_msg))
                    self._save_message(db, conversation_id, "tool", err_msg, tool_call_id=tc.id, name=tc.name)
                    continue

                # 3. Policy Tier Check
                tier = tool_def.policy.tier
                if tier in ("T2", "T3"):
                    # Generate Confirmation Card and pause execution!
                    summary = f"Execute {tool_def.name} with {json.dumps(resolved_args)}"
                    card_title = resolved_args.get("name") or resolved_args.get("title") or resolved_args.get("key")
                    card = self.policy.create_confirm_card(
                        tool=tool_def,
                        resolved_args=resolved_args,
                        preview_data={},
                        summary=summary,
                        entity_key_or_name=str(card_title) if card_title else None,
                    )
                    card_dict = asdict(card)
                    prompt_confirm = f"Action requires confirmation: {summary}"
                    self._save_message(
                        db,
                        conversation_id,
                        "assistant",
                        prompt_confirm,
                        confirm_card=card_dict,
                    )
                    return ChatTurnResult(
                        conversation_id=conversation_id,
                        reply=prompt_confirm,
                        confirm_card=card_dict,
                    )

                # 4. T0 or T1: Execute handler
                idempotency_key = self.policy.compute_idempotency_key(
                    conversation_id=conversation_id,
                    turn_index=turn_index,
                    tool_name=tc.name,
                    resolved_args=resolved_args,
                )
                cached = self.policy.check_idempotent_result(idempotency_key)
                if cached is not None:
                    tool_output = cached
                else:
                    handler = HANDLER_MAP.get(tc.name)
                    if not handler:
                        tool_output = {"error": f"Handler not implemented for {tc.name}"}
                    else:
                        try:
                            tool_output = handler(ctx, resolved_args)
                            self.policy.record_idempotent_result(idempotency_key, tool_output)
                        except Exception as e:
                            tool_output = {"error": str(e)}

                # Audit log for writes
                if tier in ("T1", "T2", "T3"):
                    self.policy.log_audit(
                        db=db,
                        user_id=int(user_id) if str(user_id).isdigit() else 0,
                        workspace_id=workspace_id,
                        tool_name=tc.name,
                        resolved_args=resolved_args,
                        outcome="success" if not isinstance(tool_output, dict) or "error" not in tool_output else "error",
                    )

                # Register Undo if T1
                undo_dict = None
                if tier == "T1" and tool_def.policy.undo:
                    undo = self.policy.register_undo(
                        tool_name=tc.name,
                        reverse_tool=tool_def.policy.undo,
                        reverse_args=resolved_args,
                        description=f"{tc.name} execution",
                    )
                    undo_dict = asdict(undo)

                executed_tools_summary.append({
                    "tool": tc.name,
                    "args": resolved_args,
                    "result": tool_output,
                })

                output_str = json.dumps(tool_output, default=str)
                llm_messages.append(
                    LLMMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=output_str)
                )
                self._save_message(
                    db,
                    conversation_id,
                    "tool",
                    output_str,
                    tool_call_id=tc.id,
                    name=tc.name,
                    undo_action=undo_dict,
                )

        # Fallback if max iterations exceeded
        return ChatTurnResult(
            conversation_id=conversation_id,
            reply="Operation completed.",
            tool_calls_executed=executed_tools_summary,
        )

    def _get_or_create_conversation(
        self, db: Session, workspace_id: str, user_id: str, conversation_id: Optional[str]
    ) -> ChatConversation:
        if conversation_id:
            conv = (
                db.query(ChatConversation)
                .filter(
                    ChatConversation.id == conversation_id,
                    ChatConversation.workspace_id == workspace_id,
                    ChatConversation.deleted == 0,
                )
                .first()
            )
            if conv:
                return conv

        conv = ChatConversation(
            id=conversation_id or None,
            workspace_id=workspace_id,
            user_id=user_id,
            title="Chat Session",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return conv

    def _save_message(
        self,
        db: Session,
        conversation_id: str,
        role: str,
        content: Optional[str],
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
        confirm_card: Optional[dict[str, Any]] = None,
        undo_action: Optional[dict[str, Any]] = None,
    ) -> ChatMessageRecord:
        msg = ChatMessageRecord(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
            confirm_card=confirm_card,
            undo_action=undo_action,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg


# Global chat gateway instance
chat_gateway = ChatGateway()
