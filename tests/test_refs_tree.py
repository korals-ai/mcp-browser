"""The refs tree as text: parsing, the interactive filter, the line-boundary
cut, value redaction and the literal `find` tier — all pure."""

from __future__ import annotations

from src.refs_tree import (
    REDACTED,
    format_matches,
    interactive_only,
    literal_matches,
    parse_line,
    redact_values,
    ref_lines,
    refs_in,
    truncate_at_line,
)
from tests.conftest import DEFAULT_TREE


def test_refs_in_and_ref_lines_follow_document_order() -> None:
    assert refs_in(DEFAULT_TREE) == {"e1", "e2", "e3", "e4"}
    lines = ref_lines(DEFAULT_TREE)
    assert list(lines) == ["e1", "e2", "e3", "e4"]
    assert lines["e4"] == '- button "Sign in" [ref=e4] [cursor=pointer]'


def test_parse_line_reads_role_name_and_ref() -> None:
    assert parse_line('  - searchbox "Search products" [ref=e2]') == {
        "indent": 2,
        "role": "searchbox",
        "name": "Search products",
        "ref": "e2",
    }
    assert parse_line("  - text: Some prose on the page") == {
        "indent": 2,
        "role": "text",
        "name": "",
        "ref": "",
    }
    assert parse_line("    - /url: /") is None  # a property line, not a node


def test_parse_line_unescapes_quotes_in_names() -> None:
    node = parse_line('- button "Say \\"hi\\"" [ref=e9]')
    assert node is not None
    assert node["name"] == 'Say "hi"'


def test_interactive_only_keeps_ref_lines_with_indentation() -> None:
    out = interactive_only(DEFAULT_TREE)
    assert out.splitlines() == [
        '  - link "Home" [ref=e1] [cursor=pointer]:',
        '  - searchbox "Search products" [ref=e2]',
        '  - textbox "Password" [ref=e3]',
        '  - button "Sign in" [ref=e4] [cursor=pointer]',
    ]


def test_truncate_at_line_cuts_on_a_line_boundary() -> None:
    text = "line one\nline two\nline three"
    body, cut = truncate_at_line(text, 14)
    assert body == "line one" and cut is True
    assert truncate_at_line(text, 100) == (text, False)
    assert truncate_at_line(text, 0) == (text, False)  # 0 = no cap


def test_redact_values_blanks_every_secret_longest_first() -> None:
    text = "pw=hunter22 and again hunter22; short=ab"
    out = redact_values(text, ["hunter22", "hunter2", "ab"])
    assert "hunter22" not in out and "hunter2" not in out
    assert out.count(REDACTED) == 2
    # A secret under the minimum length is deliberately left alone.
    assert "short=ab" in out


def test_literal_matches_are_case_insensitive_and_carry_ref_path_context() -> None:
    hits = literal_matches(DEFAULT_TREE, "sign in")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["ref"] == "e4"
    assert hit["path"] == "main"
    assert 'button "Sign in"' in str(hit["line"])
    assert any("Password" in c for c in hit["context"])  # the line above


def test_literal_matches_accept_regex_and_fall_back_on_a_bad_one() -> None:
    assert [h["ref"] for h in literal_matches(DEFAULT_TREE, r"search|sign")] == ["e2", "e4"]
    assert literal_matches(DEFAULT_TREE, "sign (") == []  # invalid regex → literal, no hit


def test_literal_matches_cap_at_twenty() -> None:
    tree = "\n".join(f'- button "b{i}" [ref=e{i}]' for i in range(40))
    assert len(literal_matches(tree, "button")) == 20


def test_format_matches_puts_the_ref_first() -> None:
    text = format_matches(literal_matches(DEFAULT_TREE, "sign in"), source="literal")
    assert text.startswith("1 match(es), source: literal")
    assert "\ne4: - button" in text
    assert "  in: main" in text
    assert format_matches([], source="literal") == "No match."
