"""Per-turn tool domain router.
Routes core tools + at most 2 selected domains per turn, ensuring bounded prompt size.
"""
from __future__ import annotations

import re
from typing import List, Set

from ai_chat.registry import ToolDefinition, registry

DOMAIN_KEYWORDS = {
    "time": [
        r"\btime\b", r"\bhours?\b", r"\bmins?\b", r"\bminutes?\b", r"\bstopwatch\b",
        r"\btimer\b", r"\btimesheets?\b", r"\bspent\b", r"\blog\b", r"\bworklog\b",
        r"\bbillable\b", r"\bclock\b",
    ],
    "planning": [
        r"\bsprints?\b", r"\biterations?\b", r"\bburndown\b", r"\bvelocity\b",
        r"\breleases?\b", r"\bversions?\b", r"\bmilestones?\b", r"\bship\b",
        r"\bchangelog\b", r"\bdeploy\b", r"\bclose sprint\b", r"\bstart sprint\b",
    ],
    "work_items": [
        r"\btickets?\b", r"\btasks?\b", r"\bbugs?\b", r"\bissues?\b", r"\bstor(y|ies)\b",
        r"\bepics?\b", r"\bcomments?\b", r"\bblocks?\b", r"\bblockers?\b",
        r"\bdependenc(y|ies)\b", r"\bclone\b", r"\bbacklog\b", r"\bboard\b",
        r"\bkanban\b", r"\bassign\b", r"\bpriorit(y|ies)\b", r"\bpoints?\b",
    ],
    "structure": [
        r"\bspaces?\b", r"\bprojects?\b", r"\bfolders?\b", r"\blists?\b",
        r"\bworkflows?\b", r"\bgovernance\b", r"\bwip\b", r"\bfreeze\b",
        r"\barchive\b", r"\bunarchive\b", r"\bunfreeze\b", r"\btenant\b",
    ],
    "reporting": [
        r"\breports?\b", r"\bexports?\b", r"\bschedules?\b", r"\bcsv\b",
        r"\bpdf\b", r"\bcfd\b", r"\bcycle\s*time\b", r"\blead\s*time\b",
        r"\bviews?\b", r"\bsaved\s*views?\b", r"\banalytics?\b",
    ],
    "admin": [
        r"\busers?\b", r"\bmembers?\b", r"\broles?\b", r"\bpermissions?\b",
        r"\bstorage\b", r"\bbucket\b", r"\bs3\b", r"\bgithub\b",
        r"\bwebhooks?\b", r"\bintegrations?\b", r"\brepositor(y|ies)\b",
    ],
}


def select_domains(user_text: str, max_domains: int = 2) -> list[str]:
    """Scores domains against user prompt and selects at most `max_domains`."""
    scores: dict[str, int] = {domain: 0 for domain in DOMAIN_KEYWORDS}
    text = user_text.lower()

    for domain, patterns in DOMAIN_KEYWORDS.items():
        for pattern in patterns:
            matches = len(re.findall(pattern, text))
            if matches > 0:
                scores[domain] += matches * 2

    # Sort domains by descending score
    sorted_domains = [d for d, s in sorted(scores.items(), key=lambda x: x[1], reverse=True) if s > 0]
    selected = sorted_domains[:max_domains]

    # If no keywords matched, default to work_items
    if not selected:
        return ["work_items"]

    return selected


def get_tools_for_turn(user_text: str) -> list[ToolDefinition]:
    """Returns core tools + tools belonging to the top 1-2 selected domains."""
    core_tools = registry.list_core_tools()
    core_names = {t.name for t in core_tools}

    domains = select_domains(user_text, max_domains=2)
    domain_tools: list[ToolDefinition] = []

    for domain in domains:
        for tool in registry.list_tools_by_domain(domain):
            if tool.name not in core_names:
                domain_tools.append(tool)
    return core_tools + domain_tools


class DomainRouter:
    def route_turn(self, user_text: str) -> list[ToolDefinition]:
        return get_tools_for_turn(user_text)

    def select_domains(self, user_text: str, max_domains: int = 2) -> list[str]:
        return select_domains(user_text, max_domains)


domain_router = DomainRouter()

