"""PMT Query Language (PQL) Parser, Validator, and SQLAlchemy Compiler.

PQL provides a structured query language for searching issues within a workspace.
It compiles safely to SQLAlchemy binary expressions with multi-tenant workspace isolation.

Grammar:
    Query       ::= Expression [ "ORDER BY" SortClause ( "," SortClause )* ]
    Expression  ::= AndExpr ( "OR" AndExpr )*
    AndExpr     ::= NotExpr ( "AND" NotExpr )*
    NotExpr     ::= [ "NOT" ] Term
    Term        ::= Comparison | "(" Expression ")"
    Comparison  ::= Field Operator Value
                  | Field "IS" [ "NOT" ] "NULL"
                  | Field [ "NOT" ] "IN" "(" ValueList ")"
                  | Field "CONTAINS" StringLiteral
    Field       ::= Identifier (e.g. status, priority, type, space, list, sprint, points, assignee)
    Operator    ::= "=" | "!=" | ">" | ">=" | "<" | "<="
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Set, Tuple

from sqlalchemy import Select, and_, desc, func, not_, or_, select
from sqlalchemy.orm import aliased

from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue
from models.sprint import Sprint
from models.user import User


class PQLError(Exception):
    """Base exception for PQL parsing or execution issues."""
    pass


class PQLSyntaxError(PQLError):
    """Raised when query fails grammatical syntax or token rules."""
    def __init__(self, message: str, position: int = 0):
        super().__init__(f"Syntax error at position {position}: {message}")
        self.message = message
        self.position = position


class PQLValidationError(PQLError):
    """Raised when field name, operator, or operand value is invalid."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- Tokens and Lexer ---

@dataclass
class Token:
    type: str
    value: Any
    pos: int


KEYWORDS = {
    "AND", "OR", "NOT", "ORDER", "BY", "ASC", "DESC", "IN", "IS", "NULL", "CONTAINS"
}

ALLOWED_FIELDS: Set[str] = {
    "space", "list", "status", "priority", "type", "assignee",
    "reporter", "sprint", "points", "story_points", "title",
    "blocked", "created_at", "due_date"
}


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch.isspace():
            i += 1
            continue

        pos = i

        # Punctuation & Parentheses
        if ch == "(":
            tokens.append(Token("LPAREN", "(", pos))
            i += 1
        elif ch == ")":
            tokens.append(Token("RPAREN", ")", pos))
            i += 1
        elif ch == ",":
            tokens.append(Token("COMMA", ",", pos))
            i += 1
        # Operators: !=, <=, >=, =, <, >
        elif ch == "!":
            if i + 1 < n and text[i + 1] == "=":
                tokens.append(Token("OPERATOR", "!=", pos))
                i += 2
            else:
                raise PQLSyntaxError("Unexpected character '!' (did you mean '!=')", pos)
        elif ch in ("<", ">"):
            if i + 1 < n and text[i + 1] == "=":
                tokens.append(Token("OPERATOR", ch + "=", pos))
                i += 2
            else:
                tokens.append(Token("OPERATOR", ch, pos))
                i += 1
        elif ch == "=":
            tokens.append(Token("OPERATOR", "=", pos))
            i += 1
        # Strings: "..." or '...'
        elif ch in ("'", '"'):
            quote = ch
            i += 1
            start_str = i
            chars = []
            while i < n:
                if text[i] == "\\":
                    if i + 1 < n:
                        chars.append(text[i + 1])
                        i += 2
                    else:
                        break
                elif text[i] == quote:
                    break
                else:
                    chars.append(text[i])
                    i += 1
            if i >= n or text[i] != quote:
                raise PQLSyntaxError("Unterminated string literal", pos)
            val = "".join(chars)
            tokens.append(Token("STRING", val, pos))
            i += 1
        # Numbers: 123, 12.5
        elif ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            start_num = i
            if ch == "-":
                i += 1
            while i < n and (text[i].isdigit() or text[i] == "."):
                i += 1
            num_str = text[start_num:i]
            val = float(num_str) if "." in num_str else int(num_str)
            tokens.append(Token("NUMBER", val, pos))
        # Identifiers & Keywords
        elif ch.isalpha() or ch == "_":
            start_id = i
            while i < n and (text[i].isalnum() or text[i] in ("_", ".")):
                i += 1
            ident = text[start_id:i]
            upper = ident.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, pos))
            else:
                tokens.append(Token("IDENTIFIER", ident.lower(), pos))
        else:
            raise PQLSyntaxError(f"Unexpected character '{ch}'", pos)

    tokens.append(Token("EOF", None, n))
    return tokens


# --- AST Nodes ---

@dataclass
class ASTNode:
    pass


@dataclass
class BinaryOp(ASTNode):
    left: ASTNode
    op: str  # "AND" | "OR"
    right: ASTNode


@dataclass
class UnaryOp(ASTNode):
    op: str  # "NOT"
    operand: ASTNode


@dataclass
class ComparisonNode(ASTNode):
    field: str
    op: str  # "=", "!=", ">", ">=", "<", "<=", "CONTAINS"
    value: Any


@dataclass
class InNode(ASTNode):
    field: str
    values: List[Any]
    negated: bool = False


@dataclass
class NullNode(ASTNode):
    field: str
    negated: bool = False


@dataclass
class SortClause:
    field: str
    descending: bool = False


@dataclass
class PQLQuery:
    filter_expr: Optional[ASTNode]
    order_by: List[SortClause]


# --- Recursive Descent Parser ---

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.cursor = 0

    def peek(self) -> Token:
        return self.tokens[self.cursor]

    def advance(self) -> Token:
        tok = self.tokens[self.cursor]
        if self.cursor < len(self.tokens) - 1:
            self.cursor += 1
        return tok

    def match_keyword(self, kw: str) -> bool:
        tok = self.peek()
        if tok.type == "KEYWORD" and tok.value == kw:
            self.advance()
            return True
        return False

    def expect_keyword(self, kw: str) -> Token:
        tok = self.peek()
        if tok.type == "KEYWORD" and tok.value == kw:
            return self.advance()
        raise PQLSyntaxError(f"Expected keyword '{kw}' but got '{tok.value}'", tok.pos)

    def parse(self) -> PQLQuery:
        filter_expr: Optional[ASTNode] = None

        if not (self.peek().type == "KEYWORD" and self.peek().value == "ORDER") and self.peek().type != "EOF":
            filter_expr = self.parse_or()

        order_by: List[SortClause] = []
        if self.match_keyword("ORDER"):
            self.expect_keyword("BY")
            order_by = self.parse_order_by()

        if self.peek().type != "EOF":
            raise PQLSyntaxError(f"Unexpected token '{self.peek().value}' after query", self.peek().pos)

        return PQLQuery(filter_expr=filter_expr, order_by=order_by)

    def parse_or(self) -> ASTNode:
        expr = self.parse_and()
        while self.match_keyword("OR"):
            right = self.parse_and()
            expr = BinaryOp(left=expr, op="OR", right=right)
        return expr

    def parse_and(self) -> ASTNode:
        expr = self.parse_not()
        while self.match_keyword("AND"):
            right = self.parse_not()
            expr = BinaryOp(left=expr, op="AND", right=right)
        return expr

    def parse_not(self) -> ASTNode:
        if self.match_keyword("NOT"):
            operand = self.parse_term()
            return UnaryOp(op="NOT", operand=operand)
        return self.parse_term()

    def parse_term(self) -> ASTNode:
        tok = self.peek()

        if tok.type == "LPAREN":
            self.advance()
            expr = self.parse_or()
            if self.peek().type != "RPAREN":
                raise PQLSyntaxError("Missing closing parenthesis ')'", self.peek().pos)
            self.advance()
            return expr

        return self.parse_comparison()

    def parse_comparison(self) -> ASTNode:
        field_tok = self.peek()
        if field_tok.type != "IDENTIFIER":
            raise PQLSyntaxError(f"Expected field name but got '{field_tok.value}'", field_tok.pos)

        field = field_tok.value
        if field not in ALLOWED_FIELDS:
            raise PQLValidationError(f"Unknown field '{field}'. Allowed fields: {', '.join(sorted(ALLOWED_FIELDS))}")

        self.advance()

        # Check for IS [NOT] NULL
        if self.match_keyword("IS"):
            negated = self.match_keyword("NOT")
            self.expect_keyword("NULL")
            return NullNode(field=field, negated=negated)

        # Check for [NOT] IN ( ... )
        negated_in = self.match_keyword("NOT")
        if self.match_keyword("IN") or (negated_in and self.match_keyword("IN")):
            if self.peek().type != "LPAREN":
                raise PQLSyntaxError("Expected '(' after IN", self.peek().pos)
            self.advance()
            values = self.parse_value_list()
            if self.peek().type != "RPAREN":
                raise PQLSyntaxError("Expected ')' after value list", self.peek().pos)
            self.advance()
            return InNode(field=field, values=values, negated=negated_in)
        elif negated_in:
            raise PQLSyntaxError("Expected 'IN' after 'NOT'", self.peek().pos)

        # Check for CONTAINS
        if self.match_keyword("CONTAINS"):
            val_tok = self.peek()
            if val_tok.type not in ("STRING", "IDENTIFIER"):
                raise PQLSyntaxError("Expected string after CONTAINS", val_tok.pos)
            self.advance()
            return ComparisonNode(field=field, op="CONTAINS", value=str(val_tok.value))

        # Standard binary comparison operator (=, !=, <, <=, >, >=)
        op_tok = self.peek()
        if op_tok.type != "OPERATOR":
            raise PQLSyntaxError(f"Expected comparison operator but got '{op_tok.value}'", op_tok.pos)

        op = op_tok.value
        self.advance()

        val_tok = self.peek()
        if val_tok.type not in ("STRING", "NUMBER", "IDENTIFIER"):
            raise PQLSyntaxError(f"Expected value after '{op}'", val_tok.pos)

        val = val_tok.value
        self.advance()
        return ComparisonNode(field=field, op=op, value=val)

    def parse_value_list(self) -> List[Any]:
        values: List[Any] = []
        tok = self.peek()
        if tok.type in ("STRING", "NUMBER", "IDENTIFIER"):
            values.append(tok.value)
            self.advance()

        while self.peek().type == "COMMA":
            self.advance()
            tok = self.peek()
            if tok.type not in ("STRING", "NUMBER", "IDENTIFIER"):
                raise PQLSyntaxError("Expected value in value list after ','", tok.pos)
            values.append(tok.value)
            self.advance()

        return values

    def parse_order_by(self) -> List[SortClause]:
        clauses: List[SortClause] = []
        while True:
            field_tok = self.peek()
            if field_tok.type != "IDENTIFIER":
                raise PQLSyntaxError(f"Expected field in ORDER BY clause but got '{field_tok.value}'", field_tok.pos)

            field = field_tok.value
            if field not in ALLOWED_FIELDS:
                raise PQLValidationError(f"Cannot sort by unknown field '{field}'")
            self.advance()

            descending = False
            if self.match_keyword("DESC"):
                descending = True
            elif self.match_keyword("ASC"):
                descending = False

            clauses.append(SortClause(field=field, descending=descending))

            if self.peek().type == "COMMA":
                self.advance()
            else:
                break
        return clauses


# --- Validation helper ---

def validate_pql(query_str: str) -> Tuple[bool, Optional[str]]:
    """Validate syntax and field semantics of a PQL query string."""
    try:
        tokens = tokenize(query_str)
        parser = Parser(tokens)
        parser.parse()
        return True, None
    except PQLError as err:
        return False, str(err)


# --- SQLAlchemy Query Compiler ---

def compile_pql_to_query(
    query_str: str,
    workspace_id: str,
    current_user_id: Optional[str] = None,
) -> Select:
    """Compile a raw PQL query string into a tenant-isolated SQLAlchemy Select statement."""
    tokens = tokenize(query_str)
    parser = Parser(tokens)
    parsed = parser.parse()

    # Base query over Issue joined with Space and WorkflowStatus for clean filtering
    stmt = (
        select(Issue)
        .outerjoin(Space, Issue.space_id == Space.id)
        .outerjoin(WorkflowStatus, Issue.status_id == WorkflowStatus.id)
        .outerjoin(ProjectList, Issue.list_id == ProjectList.id)
        .outerjoin(Sprint, Issue.sprint_id == Sprint.id)
        .outerjoin(User, Issue.assignee_id == User.id)
        .where(Issue.workspace_id == workspace_id, Issue.deleted == 0)
    )

    if parsed.filter_expr:
        compiled_clause = _compile_expr(parsed.filter_expr, current_user_id)
        if compiled_clause is not None:
            stmt = stmt.where(compiled_clause)

    # Order by
    if parsed.order_by:
        order_clauses = []
        for sort in parsed.order_by:
            col = _resolve_sort_column(sort.field)
            if col is not None:
                order_clauses.append(desc(col) if sort.descending else col)
        if order_clauses:
            stmt = stmt.order_by(*order_clauses)
    else:
        # Default sort: newest first
        stmt = stmt.order_by(Issue.created_at.desc())

    return stmt


def _compile_expr(node: ASTNode, current_user_id: Optional[str]) -> Any:
    if isinstance(node, BinaryOp):
        left_clause = _compile_expr(node.left, current_user_id)
        right_clause = _compile_expr(node.right, current_user_id)
        if node.op == "AND":
            return and_(left_clause, right_clause)
        elif node.op == "OR":
            return or_(left_clause, right_clause)

    elif isinstance(node, UnaryOp):
        if node.op == "NOT":
            return not_(_compile_expr(node.operand, current_user_id))

    elif isinstance(node, NullNode):
        col = _resolve_column(node.field)
        if col is not None:
            return col.isnot(None) if node.negated else col.is_(None)

    elif isinstance(node, InNode):
        col = _resolve_column(node.field)
        if col is not None:
            # Handle special 'assignee IN ("me")'
            resolved_vals = []
            for v in node.values:
                if node.field == "assignee" and str(v).lower() == "me" and current_user_id:
                    resolved_vals.append(current_user_id)
                else:
                    resolved_vals.append(v)
            return col.notin_(resolved_vals) if node.negated else col.in_(resolved_vals)

    elif isinstance(node, ComparisonNode):
        return _compile_comparison(node, current_user_id)

    return None


def _resolve_column(field: str) -> Any:
    f = field.lower()
    if f in ("points", "story_points"):
        return Issue.story_points
    elif f == "type":
        return Issue.type
    elif f == "priority":
        return Issue.priority
    elif f == "blocked":
        return Issue.blocked
    elif f == "title":
        return Issue.title
    elif f == "created_at":
        return Issue.created_at
    elif f == "due_date":
        return Issue.due_date
    elif f == "assignee":
        return Issue.assignee_id
    elif f == "reporter":
        return Issue.reporter_id
    elif f == "space":
        return Space.key
    elif f == "list":
        return ProjectList.name
    elif f == "status":
        return WorkflowStatus.category
    elif f == "sprint":
        return Sprint.name
    return None


def _resolve_sort_column(field: str) -> Any:
    f = field.lower()
    if f in ("points", "story_points"):
        return Issue.story_points
    elif f == "priority":
        return Issue.priority
    elif f == "created_at":
        return Issue.created_at
    elif f == "due_date":
        return Issue.due_date
    elif f == "title":
        return Issue.title
    return Issue.created_at


def _compile_comparison(node: ComparisonNode, current_user_id: Optional[str]) -> Any:
    f = node.field.lower()
    op = node.op
    val = node.value

    # 1. Assignee special resolution: "me" -> current_user_id
    if f == "assignee":
        if str(val).lower() == "me" and current_user_id:
            val = current_user_id
        if op == "=":
            return Issue.assignee_id == val
        elif op == "!=":
            return Issue.assignee_id != val

    # 2. Status matching by Category OR Name
    if f == "status":
        sval = str(val)
        if op == "=":
            return or_(WorkflowStatus.category == sval.lower(), func.lower(WorkflowStatus.name) == sval.lower())
        elif op == "!=":
            return and_(WorkflowStatus.category != sval.lower(), func.lower(WorkflowStatus.name) != sval.lower())

    # 3. Space matching by Space Key or Space Name
    if f == "space":
        sval = str(val)
        if op == "=":
            return or_(func.upper(Space.key) == sval.upper(), func.lower(Space.name) == sval.lower())
        elif op == "!=":
            return and_(func.upper(Space.key) != sval.upper(), func.lower(Space.name) != sval.lower())

    # 4. Sprint matching by Sprint Name or State (active/planning/completed)
    if f == "sprint":
        sval = str(val)
        if op == "=":
            return or_(func.lower(Sprint.status) == sval.lower(), func.lower(Sprint.name) == sval.lower())
        elif op == "!=":
            return and_(func.lower(Sprint.status) != sval.lower(), func.lower(Sprint.name) != sval.lower())

    # 5. List matching by List Name
    if f == "list":
        sval = str(val)
        if op == "=":
            return func.lower(ProjectList.name) == sval.lower()
        elif op == "!=":
            return func.lower(ProjectList.name) != sval.lower()

    # 6. Text containment
    if op == "CONTAINS" or (f == "title" and op in ("=", "CONTAINS")):
        col = _resolve_column(f)
        if col is not None:
            return col.ilike(f"%{val}%")

    # 7. Standard scalar comparison
    col = _resolve_column(f)
    if col is not None:
        if op == "=":
            return col == val
        elif op == "!=":
            return col != val
        elif op == ">":
            return col > val
        elif op == ">=":
            return col >= val
        elif op == "<":
            return col < val
        elif op == "<=":
            return col <= val

    return None
