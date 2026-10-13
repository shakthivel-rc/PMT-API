"""Registry of tools for the AI Chat Gateway.
Loads tool_registry.json and tool_schemas_enabled.json, exposing only enabled tools
and virtual tools with stripped metadata for model providers.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Locate spec files
SPEC_DIR = Path(__file__).resolve().parent / "spec"
if not SPEC_DIR.exists():
    SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"

TOOL_REGISTRY_PATH = SPEC_DIR / "tool_registry.json"
TOOL_SCHEMAS_ENABLED_PATH = SPEC_DIR / "tool_schemas_enabled.json"


@dataclass
class ToolPolicy:
    tier: str  # T0, T1, T2, T3
    confirm: str  # none, user_pref, always, typed
    tier_note: str = ""
    catalog_risk: str = ""
    risk_note: str = ""
    preview_reads: list[str] = field(default_factory=list)
    undo: Optional[str] = None


@dataclass
class ToolDefinition:
    id: str
    name: str
    module: str
    domain: str
    entity: str
    verb: str
    core: bool
    exposure: str
    exposure_reason: str
    policy: ToolPolicy
    permission_required: str
    description: str
    parameters: dict[str, Any]
    injected_by_gateway: list[str]
    resolvers: dict[str, str]
    api: dict[str, str]
    preconditions: str = ""
    side_effects: str = ""
    errors: str = ""
    synonyms: list[str] = field(default_factory=list)
    example_phrasings: list[str] = field(default_factory=list)
    is_virtual: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._meta: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not TOOL_REGISTRY_PATH.exists():
            raise FileNotFoundError(f"Tool registry file not found at {TOOL_REGISTRY_PATH}")

        with open(TOOL_REGISTRY_PATH, "r", encoding="utf-8") as f:
            registry_data = json.load(f)

        self._meta = registry_data.get("meta", {})

        # Load enabled tools from tool_registry.json
        for raw in registry_data.get("tools", []):
            if raw.get("exposure") != "enabled":
                continue

            policy_dict = raw.get("policy", {})
            policy = ToolPolicy(
                tier=policy_dict.get("tier", "T0"),
                confirm=policy_dict.get("confirm", "none"),
                tier_note=policy_dict.get("tier_note", ""),
                catalog_risk=policy_dict.get("catalog_risk", ""),
                risk_note=policy_dict.get("risk_note", ""),
                preview_reads=policy_dict.get("preview_reads", []) or [],
                undo=policy_dict.get("undo"),
            )

            tool = ToolDefinition(
                id=raw["id"],
                name=raw["id"],  # Function name matches tool id
                module=raw.get("module", ""),
                domain=raw.get("domain", "work_items"),
                entity=raw.get("entity", ""),
                verb=raw.get("verb", ""),
                core=raw.get("core", False),
                exposure=raw.get("exposure", "enabled"),
                exposure_reason=raw.get("exposure_reason", ""),
                policy=policy,
                permission_required=raw.get("permission_required", ""),
                description=raw.get("description", ""),
                parameters=raw.get("parameters", {"type": "object", "properties": {}}),
                injected_by_gateway=raw.get("injected_by_gateway", []),
                resolvers=raw.get("resolvers", {}),
                api=raw.get("api", {}),
                preconditions=raw.get("preconditions", ""),
                side_effects=raw.get("side_effects", ""),
                errors=raw.get("errors", ""),
                synonyms=raw.get("synonyms", []),
                example_phrasings=raw.get("example_phrasings", []),
                is_virtual=False,
            )
            self._tools[tool.name] = tool

        # Also load tool_schemas_enabled.json to get virtual tools & overrides
        if TOOL_SCHEMAS_ENABLED_PATH.exists():
            with open(TOOL_SCHEMAS_ENABLED_PATH, "r", encoding="utf-8") as f:
                schemas_data = json.load(f)

            for raw_schema in schemas_data.get("tools", []):
                name = raw_schema["name"]
                meta = raw_schema.get("_meta", {})
                domain = meta.get("domain", "work_items")
                tier = meta.get("tier", "T0")
                core = meta.get("core", False)

                # If this tool is not yet in registry (i.e. it's a virtual tool)
                if name not in self._tools:
                    # Parse tier
                    clean_tier = tier.split()[0] if tier else "T0"
                    confirm = "always" if clean_tier == "T2" else ("typed" if clean_tier == "T3" else "none")

                    policy = ToolPolicy(
                        tier=clean_tier,
                        confirm=confirm,
                        tier_note=tier,
                    )

                    tool = ToolDefinition(
                        id=name,
                        name=name,
                        module="virtual",
                        domain=domain,
                        entity="virtual",
                        verb="execute",
                        core=core,
                        exposure="enabled",
                        exposure_reason="Virtual gateway tool",
                        policy=policy,
                        permission_required="None" if clean_tier == "T0" else "Workspace member",
                        description=raw_schema.get("description", ""),
                        parameters=raw_schema.get("parameters", {"type": "object", "properties": {}}),
                        injected_by_gateway=["workspace_id"],
                        resolvers={},
                        api={"service": f"virtual:{name}"},
                        is_virtual=True,
                    )
                    self._tools[name] = tool
                else:
                    # Update core/parameters if needed from schemas_enabled
                    existing = self._tools[name]
                    if core:
                        existing.core = True
                    if domain:
                        existing.domain = domain

    @property
    def meta(self) -> dict[str, Any]:
        return self._meta

    @property
    def domains(self) -> dict[str, str]:
        return self._meta.get("domains", {
            "work_items": "issues, comments, dependencies",
            "planning": "sprints, releases",
            "time": "time entries, timers",
            "structure": "workspace, spaces/folders/lists, governance, workflow",
            "reporting": "reports, saved views, activity",
            "admin": "platform users, roles, storage, integrations",
        })

    def get_tool(self, name_or_id: str) -> Optional[ToolDefinition]:
        return self._tools.get(name_or_id)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def list_core_tools(self) -> list[ToolDefinition]:
        return [t for t in self._tools.values() if t.core]

    def list_tools_by_domain(self, domain: str) -> list[ToolDefinition]:
        return [t for t in self._tools.values() if t.domain == domain]

    def get_provider_schema(self, tool: ToolDefinition) -> dict[str, Any]:
        """Returns JSON schema for LLM provider, strictly omitting internal metadata
        and parameters injected by the gateway."""
        params = json.loads(json.dumps(tool.parameters))
        props = params.get("properties", {})
        reqs = set(params.get("required", []))

        # Remove injected parameters (like workspace_id) from model visibility
        for injected in tool.injected_by_gateway:
            if injected in props:
                del props[injected]
            reqs.discard(injected)

        params["required"] = sorted(list(reqs))
        params["properties"] = props

        return {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        }


    def get(self, name_or_id: str) -> Optional[ToolDefinition]:
        return self._tools.get(name_or_id)

    def get_tools_schema_for_provider(
        self, provider: str = "openai", tools: Optional[list[ToolDefinition]] = None
    ) -> list[dict[str, Any]]:
        tool_list = tools if tools is not None else self.list_tools()
        return [self.get_provider_schema(t) for t in tool_list]


# Singleton registry instance
registry = ToolRegistry()
TOOL_REGISTRY = registry

