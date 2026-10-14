"""Unit tests for PQL Lexer, Parser, and Compiler."""
import pytest
from utils.pql import (
    BinaryOp,
    ComparisonNode,
    InNode,
    NullNode,
    PQLQuery,
    PQLSyntaxError,
    PQLValidationError,
    Parser,
    UnaryOp,
    compile_pql_to_query,
    tokenize,
    validate_pql,
)


def test_tokenize_basic():
    tokens = tokenize('space = "PAY" AND points >= 5')
    types = [t.type for t in tokens]
    assert types == ["IDENTIFIER", "OPERATOR", "STRING", "KEYWORD", "IDENTIFIER", "OPERATOR", "NUMBER", "EOF"]
    assert tokens[0].value == "space"
    assert tokens[1].value == "="
    assert tokens[2].value == "PAY"
    assert tokens[3].value == "AND"
    assert tokens[6].value == 5


def test_tokenize_parentheses_and_commas():
    tokens = tokenize('priority IN ("urgent", "high")')
    types = [t.type for t in tokens]
    assert types == ["IDENTIFIER", "KEYWORD", "LPAREN", "STRING", "COMMA", "STRING", "RPAREN", "EOF"]


def test_parser_simple_comparison():
    query = Parser(tokenize('type = "bug"')).parse()
    assert isinstance(query.filter_expr, ComparisonNode)
    assert query.filter_expr.field == "type"
    assert query.filter_expr.op == "="
    assert query.filter_expr.value == "bug"
    assert len(query.order_by) == 0


def test_parser_operator_precedence_and_over_or():
    # a OR b AND c should parse as a OR (b AND c)
    query = Parser(tokenize('priority = "urgent" OR type = "bug" AND points > 3')).parse()
    assert isinstance(query.filter_expr, BinaryOp)
    assert query.filter_expr.op == "OR"
    assert isinstance(query.filter_expr.left, ComparisonNode)
    assert query.filter_expr.left.field == "priority"

    right = query.filter_expr.right
    assert isinstance(right, BinaryOp)
    assert right.op == "AND"
    assert right.left.field == "type"
    assert right.right.field == "points"


def test_parser_parentheses_override_precedence():
    # (a OR b) AND c
    query = Parser(tokenize('(priority = "urgent" OR type = "bug") AND points > 3')).parse()
    assert isinstance(query.filter_expr, BinaryOp)
    assert query.filter_expr.op == "AND"
    assert isinstance(query.filter_expr.left, BinaryOp)
    assert query.filter_expr.left.op == "OR"


def test_parser_is_null_and_in():
    query = Parser(tokenize('assignee IS NULL AND priority IN ("urgent", "high")')).parse()
    assert isinstance(query.filter_expr, BinaryOp)
    assert isinstance(query.filter_expr.left, NullNode)
    assert query.filter_expr.left.field == "assignee"
    assert query.filter_expr.left.negated is False

    assert isinstance(query.filter_expr.right, InNode)
    assert query.filter_expr.right.field == "priority"
    assert query.filter_expr.right.values == ["urgent", "high"]


def test_parser_order_by():
    query = Parser(tokenize('status = "in_progress" ORDER BY points DESC, created_at ASC')).parse()
    assert len(query.order_by) == 2
    assert query.order_by[0].field == "points"
    assert query.order_by[0].descending is True
    assert query.order_by[1].field == "created_at"
    assert query.order_by[1].descending is False


def test_syntax_errors():
    with pytest.raises(PQLSyntaxError):
        Parser(tokenize('status = ')).parse()

    with pytest.raises(PQLSyntaxError):
        Parser(tokenize('(status = "done"')).parse()  # missing closing paren


def test_validation_errors():
    with pytest.raises(PQLValidationError):
        Parser(tokenize('invalid_col = 123')).parse()

    is_valid, err = validate_pql('non_existent_field = "xyz"')
    assert is_valid is False
    assert "Unknown field" in (err or "")


def test_validate_pql_success():
    is_valid, err = validate_pql('space = "PAY" AND type = "bug" AND priority != "low"')
    assert is_valid is True
    assert err is None


def test_compile_pql_to_query():
    stmt = compile_pql_to_query(
        'type = "bug" AND points >= 5 ORDER BY points DESC',
        workspace_id="ws-123",
        current_user_id="user-456",
    )
    sql = str(stmt)
    assert "issues.workspace_id = :workspace_id_1" in sql
    assert "issues.deleted = :deleted_1" in sql
