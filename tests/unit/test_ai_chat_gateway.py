"""Comprehensive unit tests for the AI Chat Gateway:
- Tool Registry (exposure, stripping injected workspace_id)
- Domain Router (core tools + <= 2 domains)
- Resolvers (space, list, sprint, member, issue, disambiguation)
- Virtual Tools (ask_user, find_*, knowledge_search, issue_clone, issue_bulk_update)
- Policy Engine T0-T3 (T0 immediate, T1 undo, T2 confirm card, T3 typed confirm, idempotency, audit)
- LLM Adapters (OpenAI, Anthropic, Google, Mock, per-user settings)
- Gateway Turn Execution & API Routes
"""
import uuid
import pytest
from unittest.mock import MagicMock

from ai_chat.domain_router import domain_router, select_domains
from ai_chat.gateway import chat_gateway
from ai_chat.handlers import HANDLER_MAP
from ai_chat.handlers.context import ToolContext
from ai_chat.handlers.virtual_handlers import (
    handle_ask_user,
    handle_find_issue,
    handle_find_list,
    handle_find_member,
    handle_find_space,
    handle_find_sprint,
    handle_knowledge_search,
)
from ai_chat.llm.anthropic_adapter import AnthropicAdapter
from ai_chat.llm.base import LLMMessage, LLMResult, LLMToolCall
from ai_chat.llm.google_adapter import GoogleAdapter
from ai_chat.llm.mock_adapter import MockLLMAdapter
from ai_chat.llm.openai_adapter import OpenAIAdapter
from ai_chat.llm.settings import (
    delete_user_llm_settings,
    get_user_llm_settings,
    save_user_llm_settings,
)
from ai_chat.policy_engine import policy_engine
from ai_chat.rbac import check_tool_permission
from ai_chat.registry import TOOL_REGISTRY
from ai_chat.resolvers import (
    resolve_issue,
    resolve_list,
    resolve_member,
    resolve_space,
    resolve_sprint,
)
from dependencies.workspace import WorkspaceAccess
from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue
from models.sprint import Sprint
from models.user import User
from models.workspace import Workspace, WorkspaceMember


# =========================================================================
# 1. Tool Registry Tests
# =========================================================================

def test_registry_loads_enabled_tools_only():
    tools = TOOL_REGISTRY.list_tools()
    assert len(tools) == 122
    for t in tools:
        assert t.exposure == "enabled"


def test_registry_strips_injected_workspace_id_from_schema():
    tool = TOOL_REGISTRY.get("issue_create")
    assert tool is not None
    schema = TOOL_REGISTRY.get_provider_schema(tool)
    props = schema["parameters"]["properties"]
    assert "workspace_id" not in props
    assert "workspace_id" not in schema["parameters"].get("required", [])


def test_registry_has_virtual_tools():
    virtual_names = [
        "ask_user", "find_space", "find_list", "find_sprint",
        "find_member", "find_issue", "knowledge_search",
        "issue_clone", "issue_bulk_update",
    ]
    for vname in virtual_names:
        vtool = TOOL_REGISTRY.get(vname)
        assert vtool is not None, f"Missing virtual tool: {vname}"
        assert vtool.is_virtual is True
        assert vname in HANDLER_MAP


# =========================================================================
# 2. Domain Router Tests
# =========================================================================

def test_domain_router_selects_at_most_two_domains():
    domains = select_domains("Show me timesheets and sprints for the project", max_domains=2)
    assert len(domains) <= 2
    assert "time" in domains or "planning" in domains or "structure" in domains


def test_domain_router_bounds_tools_per_turn():
    tools = domain_router.route_turn("Log 2 hours on ticket XYZ-104")
    assert len(tools) > 0
    # Core tools + time/work_items domain tools should be <= 50
    assert len(tools) <= 50
    core_tools = TOOL_REGISTRY.list_core_tools()
    for ct in core_tools:
        assert ct in tools


# =========================================================================
# 3. Resolvers & Disambiguation Tests
# =========================================================================

def test_resolvers(db):
    u1 = User(id=str(uuid.uuid4()), username="sarah_khan", email="sarah.k@example.com", first_name="Sarah", last_name="Khan", organization="Acme", department="Eng")
    u2 = User(id=str(uuid.uuid4()), username="sarah_lee", email="sarah.l@example.com", first_name="Sarah", last_name="Lee", organization="Acme", department="Eng")
    db.add_all([u1, u2])
    db.flush()

    ws_id = str(uuid.uuid4())
    ws = Workspace(id=ws_id, name="Acme Corp", slug=f"acme-{uuid.uuid4().hex[:6]}", created_by=u1.id)
    db.add(ws)
    db.flush()

    m1 = WorkspaceMember(workspace_id=ws_id, user_id=u1.id, role="member")
    m2 = WorkspaceMember(workspace_id=ws_id, user_id=u2.id, role="member")
    db.add_all([m1, m2])

    # Space & List
    sp = Space(id=str(uuid.uuid4()), workspace_id=ws_id, name="Billing", key="BIL")
    db.add(sp)
    db.flush()

    pl = ProjectList(id=str(uuid.uuid4()), workspace_id=ws_id, space_id=sp.id, name="Backlog", position=1000)
    db.add(pl)

    # Sprint
    sp_sprint = Sprint(id=str(uuid.uuid4()), workspace_id=ws_id, space_id=sp.id, name="Sprint 13", state="active", created_by=u1.id)
    db.add(sp_sprint)

    # Workflow Status & Issue
    st = WorkflowStatus(id=str(uuid.uuid4()), workspace_id=ws_id, space_id=sp.id, name="To Do", category="todo", position=1000)
    db.add(st)
    db.flush()

    iss = Issue(
        id=str(uuid.uuid4()),
        workspace_id=ws_id,
        space_id=sp.id,
        list_id=pl.id,
        status_id=st.id,
        number=104,
        title="Fix login bug",
        reporter_id=u1.id,
        rank="0|hzzzzz:",
        backlog_rank="0|hzzzzz:",
    )
    db.add(iss)
    db.commit()

    # 1. Space resolver
    res_sp = resolve_space(db, ws_id, "BIL")
    assert res_sp.success is True
    assert res_sp.value == sp.id

    res_sp_name = resolve_space(db, ws_id, "Billing")
    assert res_sp_name.success is True
    assert res_sp_name.value == sp.id

    # Composite space name tests
    res_sp_comp1 = resolve_space(db, ws_id, "billing-bil")
    assert res_sp_comp1.success is True
    assert res_sp_comp1.value == sp.id

    res_sp_comp2 = resolve_space(db, ws_id, "Billing · BIL")
    assert res_sp_comp2.success is True
    assert res_sp_comp2.value == sp.id

    res_sp_comp3 = resolve_space(db, ws_id, "Billing - BIL")
    assert res_sp_comp3.success is True
    assert res_sp_comp3.value == sp.id

    # 2. List resolver auto-selection (Rule 3)
    res_list = resolve_list(db, ws_id, sp.id, None)
    assert res_list.success is True
    assert res_list.value == pl.id

    # 3. Sprint resolver
    res_sprint = resolve_sprint(db, ws_id, sp.id, "Sprint 13")
    assert res_sprint.success is True
    assert res_sprint.value == sp_sprint.id

    res_sprint_num = resolve_sprint(db, ws_id, sp.id, "13")
    assert res_sprint_num.success is True
    assert res_sprint_num.value == sp_sprint.id

    # 4. Member resolver exact & ambiguous
    res_m1 = resolve_member(db, ws_id, "Sarah Khan")
    assert res_m1.success is True
    assert res_m1.value == u1.id

    # Ambiguous "Sarah" should return disambiguation
    res_ambig = resolve_member(db, ws_id, "Sarah")
    assert res_ambig.success is False
    assert res_ambig.is_ambiguous is True
    assert len(res_ambig.candidates) == 2

    # Self reference
    res_self = resolve_member(db, ws_id, "$self", caller_id=u1.id)
    assert res_self.success is True
    assert res_self.value == u1.id

    # 5. Issue resolver
    res_issue_key = resolve_issue(db, ws_id, "BIL-104")
    assert res_issue_key.success is True
    assert res_issue_key.value == iss.id


# =========================================================================
# 4. Virtual Handlers Tests
# =========================================================================

def test_virtual_handlers(db):
    u = User(id=str(uuid.uuid4()), username="testuser", email="test@example.com", first_name="Test", last_name="User", organization="Org", department="IT")
    db.add(u)
    db.flush()

    ws_id = str(uuid.uuid4())
    ws = Workspace(id=ws_id, name="Test WS", slug=f"ws-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)

    wm = WorkspaceMember(workspace_id=ws_id, user_id=u.id, role="admin")
    db.add(wm)
    db.commit()

    access = WorkspaceAccess(workspace_id=ws_id, user_id=u.id, role="admin")
    ctx = ToolContext(db=db, workspace_id=ws_id, current_user=u, access=access, role="admin")

    # ask_user
    res_ask = handle_ask_user(ctx, {"question": "Which list?", "options": ["Backlog", "Bugs"]})
    assert res_ask["action"] == "ask_user"
    assert res_ask["question"] == "Which list?"
    assert res_ask["options"] == ["Backlog", "Bugs"]

    # find_space
    sp = Space(id=str(uuid.uuid4()), workspace_id=ws_id, name="Frontend", key="FE")
    db.add(sp)
    db.flush()

    pl = ProjectList(id=str(uuid.uuid4()), workspace_id=ws_id, space_id=sp.id, name="General", position=1000)
    st = WorkflowStatus(id=str(uuid.uuid4()), workspace_id=ws_id, space_id=sp.id, name="To Do", category="todo", position=1000)
    db.add_all([pl, st])
    db.flush()

    iss = Issue(
        id=str(uuid.uuid4()),
        workspace_id=ws_id,
        space_id=sp.id,
        list_id=pl.id,
        status_id=st.id,
        number=1,
        title="Frontend redesign",
        description="Refactoring the Frontend UI components",
        reporter_id=u.id,
        rank="0|hzzzzz:",
        backlog_rank="0|hzzzzz:",
    )
    db.add(iss)
    db.commit()

    res_find_sp = handle_find_space(ctx, {"query": "Front"})
    assert len(res_find_sp) == 1
    assert res_find_sp[0]["name"] == "Frontend"

    # find_member
    res_find_m = handle_find_member(ctx, {"query": "test"})
    assert len(res_find_m) >= 1
    assert res_find_m[0]["username"] == "testuser"

    # knowledge_search
    res_ks = handle_knowledge_search(ctx, {"query": "Frontend"})
    assert isinstance(res_ks, list)
    assert len(res_ks) >= 1


# =========================================================================
# 5. Policy Engine & RBAC Tests
# =========================================================================

def test_rbac_check():
    # Viewer cannot write
    allowed, err = check_tool_permission("issue_create", "viewer")
    assert allowed is False
    assert "viewer" in err.lower()

    # Member can create issue
    allowed, err = check_tool_permission("issue_create", "member")
    assert allowed is True

    # Member cannot create space (admin only)
    allowed, err = check_tool_permission("space_create", "member")
    assert allowed is False


def test_policy_engine_t0_t1_t2_t3():
    # T0 check
    t0_tool = TOOL_REGISTRY.get("time_entries_list")
    assert t0_tool.policy.tier == "T0"

    # T1 check
    t1_tool = TOOL_REGISTRY.get("issue_create")
    assert t1_tool.policy.tier == "T1"

    # T2 confirm card
    t2_tool = TOOL_REGISTRY.get("space_create")
    assert t2_tool.policy.tier == "T2"
    card = policy_engine.create_confirm_card(
        tool=t2_tool,
        resolved_args={"name": "Billing"},
        preview_data={},
        summary="Create space Billing",
    )
    assert card.requires_typed_confirmation is False
    ok, validated_card, err = policy_engine.validate_confirmation(card.confirm_id)
    assert ok is True
    assert validated_card.summary == "Create space Billing"

    # T3 typed confirmation
    t3_tool = TOOL_REGISTRY.get("issue_delete")
    assert t3_tool.policy.tier == "T3"
    t3_card = policy_engine.create_confirm_card(
        tool=t3_tool,
        resolved_args={"issue_id": "iss_123"},
        preview_data={},
        summary="Delete issue XYZ-104",
        entity_key_or_name="XYZ-104",
    )
    assert t3_card.requires_typed_confirmation is True
    assert t3_card.expected_typed_text == "XYZ-104"

    # Fail validation if typed text does not match
    ok_fail, _, err = policy_engine.validate_confirmation(t3_card.confirm_id, typed_text="WrongName")
    assert ok_fail is False
    assert "does not match" in err

    # Succeed validation with correct typed text
    ok_pass, _, _ = policy_engine.validate_confirmation(t3_card.confirm_id, typed_text="XYZ-104")
    assert ok_pass is True


def test_policy_engine_undo_and_idempotency():
    # Undo registration
    undo = policy_engine.register_undo(
        tool_name="issue_create",
        reverse_tool="issue_delete",
        reverse_args={"issue_id": "iss_1"},
        description="Created issue iss_1",
    )
    assert undo.action_id.startswith("undo_")
    found = policy_engine.get_undo(undo.action_id)
    assert found is not None
    assert found.reverse_tool == "issue_delete"

    # Idempotency
    key = policy_engine.compute_idempotency_key("conv1", 1, "issue_create", {"title": "Test"})
    assert key is not None
    assert policy_engine.check_idempotent_result(key) is None
    policy_engine.record_idempotent_result(key, {"id": "iss_123"})
    assert policy_engine.check_idempotent_result(key) == {"id": "iss_123"}


# =========================================================================
# 6. LLM Adapters & User Settings Tests
# =========================================================================

def test_user_llm_settings_and_encryption(db):
    u = User(id=str(uuid.uuid4()), username="ai_user", email="ai@example.com", first_name="AI", last_name="User", organization="Acme", department="Eng")
    db.add(u)
    db.commit()

    saved = save_user_llm_settings(
        db=db,
        user_id=u.id,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="sk-ant-secret-key-12345",
        base_url="https://api.anthropic.com/v1",
    )
    assert saved.provider == "anthropic"
    assert saved.encrypted_api_key != "sk-ant-secret-key-12345"
    assert saved.get_api_key() == "sk-ant-secret-key-12345"

    retrieved = get_user_llm_settings(db, u.id)
    assert retrieved is not None
    assert retrieved.get_api_key() == "sk-ant-secret-key-12345"

    deleted = delete_user_llm_settings(db, u.id)
    assert deleted is True
    assert get_user_llm_settings(db, u.id) is None


def test_openai_adapter_payload():
    adapter = OpenAIAdapter(api_key="test-key", model="gpt-4o")
    msgs = [
        LLMMessage(role="user", content="Hello"),
    ]
    tools = [
        {"name": "time_entries_list", "description": "List time", "parameters": {"type": "object"}},
    ]
    payload = adapter._build_payload(msgs, tools, 0.2, 1024)
    assert payload["model"] == "gpt-4o"
    assert payload["tools"][0]["function"]["name"] == "time_entries_list"
    assert payload["messages"][0]["content"] == "Hello"


def test_anthropic_adapter_payload():
    adapter = AnthropicAdapter(api_key="test-key", model="claude-3-5-sonnet-20241022")
    msgs = [
        LLMMessage(role="system", content="System instruction"),
        LLMMessage(role="user", content="Hi Claude"),
    ]
    tools = [
        {"name": "time_entries_list", "description": "List time", "parameters": {"type": "object"}},
    ]
    payload = adapter._build_payload(msgs, tools, 0.2, 1024)
    assert payload["system"] == "System instruction"
    assert payload["tools"][0]["name"] == "time_entries_list"
    assert payload["messages"][0]["role"] == "user"


def test_google_adapter_payload():
    adapter = GoogleAdapter(api_key="test-key", model="gemini-1.5-pro")
    msgs = [
        LLMMessage(role="system", content="System prompt"),
        LLMMessage(role="user", content="Hi Gemini"),
    ]
    tools = [
        {"name": "time_entries_list", "description": "List time", "parameters": {"type": "object"}},
    ]
    payload = adapter._build_payload(msgs, tools, 0.2, 1024)
    assert payload["systemInstruction"]["parts"][0]["text"] == "System prompt"
    assert payload["contents"][0]["role"] == "user"
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "time_entries_list"


# =========================================================================
# 7. Gateway Orchestration End-to-End Tests
# =========================================================================

def test_gateway_immediate_read_turn(db):
    u = User(id=str(uuid.uuid4()), username="priya_nair", email="priya@example.com", first_name="Priya", last_name="Nair", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws_id = str(uuid.uuid4())
    ws = Workspace(id=ws_id, name="Test WS", slug=f"ws-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)
    db.flush()

    wm = WorkspaceMember(workspace_id=ws_id, user_id=u.id, role="member")
    db.add(wm)
    db.commit()

    access = WorkspaceAccess(workspace_id=ws_id, user_id=u.id, role="member")

    # Mock adapter simulating tool call time_entries_list -> then text response
    mock_adapter = MockLLMAdapter(
        canned_responses=[
            LLMResult(
                content=None,
                tool_calls=[
                    LLMToolCall(
                        id="call_1",
                        name="time_entries_list",
                        arguments={"from_date": "2026-10-01", "to_date": "2026-10-10"},
                    )
                ],
            ),
            LLMResult(content="Found 0 time entries between 1 Oct and 10 Oct 2026."),
        ]
    )

    result = chat_gateway.execute_turn(
        db=db,
        access=access,
        current_user=u,
        message="List all my timesheets from 01-10-2026 to 10-10-2026",
        adapter=mock_adapter,
    )

    assert result.error is None
    assert result.reply == "Found 0 time entries between 1 Oct and 10 Oct 2026."
    assert len(result.tool_calls_executed) == 1
    assert result.tool_calls_executed[0]["tool"] == "time_entries_list"


def test_gateway_t2_confirmation_card_generation(db):
    u = User(id=str(uuid.uuid4()), username="alex_kumar", email="alex@example.com", first_name="Alex", last_name="Kumar", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws_id = str(uuid.uuid4())
    ws = Workspace(id=ws_id, name="Test WS", slug=f"ws-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)
    db.flush()

    wm = WorkspaceMember(workspace_id=ws_id, user_id=u.id, role="admin")
    db.add(wm)
    db.commit()

    access = WorkspaceAccess(workspace_id=ws_id, user_id=u.id, role="admin")

    # Mock adapter proposing space_create
    mock_adapter = MockLLMAdapter(
        canned_responses=[
            LLMResult(
                content=None,
                tool_calls=[
                    LLMToolCall(
                        id="call_sp",
                        name="space_create",
                        arguments={"name": "Billing"},
                    )
                ],
            )
        ]
    )

    result = chat_gateway.execute_turn(
        db=db,
        access=access,
        current_user=u,
        message="Create a project named Billing",
        adapter=mock_adapter,
    )

    assert result.confirm_card is not None
    assert result.confirm_card["tool_name"] == "space_create"
    assert result.confirm_card["tier"] == "T2"
    assert result.confirm_card["resolved_args"]["name"] == "Billing"
    assert len(result.tool_calls_executed) == 0  # Not executed yet!

    # Now confirm the action!
    confirm_res = chat_gateway.execute_turn(
        db=db,
        access=access,
        current_user=u,
        confirm_action_id=result.confirm_card["confirm_id"],
        conversation_id=result.conversation_id,
    )

    assert confirm_res.error is None
    assert len(confirm_res.tool_calls_executed) == 1
    assert confirm_res.tool_calls_executed[0]["tool"] == "space_create"


def test_gateway_ask_user_pauses_execution(db):
    u = User(id=str(uuid.uuid4()), username="test_asker", email="asker@example.com", first_name="Test", last_name="Asker", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws_id = str(uuid.uuid4())
    ws = Workspace(id=ws_id, name="Test Ask WS", slug=f"ws-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)
    db.flush()

    wm = WorkspaceMember(workspace_id=ws_id, user_id=u.id, role="admin")
    db.add(wm)
    db.commit()

    access = WorkspaceAccess(workspace_id=ws_id, user_id=u.id, role="admin")

    mock_adapter = MockLLMAdapter(
        canned_responses=[
            LLMResult(
                content=None,
                tool_calls=[
                    LLMToolCall(
                        id="call_ask",
                        name="ask_user",
                        arguments={
                            "question": "Which list should I create this under?",
                            "options": ["UPI", "Dashboard"],
                        },
                    )
                ],
            )
        ]
    )

    result = chat_gateway.execute_turn(
        db=db,
        access=access,
        current_user=u,
        message="Create ticket under features",
        adapter=mock_adapter,
    )

    assert result.error is None
    assert result.reply == "Which list should I create this under?"
    assert result.clarification is not None
    assert result.clarification["tool_name"] == "ask_user"
    assert len(result.clarification["candidates"]) == 2

