"""The accessibility tree with refs, as text — parsing, filtering, redaction.

The agent reads a page through Playwright's ``aria_snapshot(mode="ai")``: one
line per node, ``- role "name" [ref=e12] [cursor=pointer]: text``, indented two
spaces per level, with ``- /url: …`` property lines under links. A ref is a
handle Playwright assigns once per document and keeps while the node's role
and name hold, so it survives a re-read and a keystroke (measured 287/287 on
a live retail home page, 2026-09-15), and dies with a navigation.

Pure module: every function here is over the tree TEXT, so the filters, the
literal ``find`` tier, the truncation rule and the value redaction are all
unit-testable without a browser. The driver produces the tree; the tool
handlers shape it with these.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_REF_RE = re.compile(r"\[ref=([A-Za-z0-9_-]+)\]")
_LINE_RE = re.compile(r'^(?P<indent>\s*)- (?P<role>[A-Za-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?')

# A secret shorter than this is not redacted: blanking every "ab" in a page
# would destroy the page, and a two-character password protects nothing a
# redaction could save. Stated, not silent.
MIN_SECRET_LEN = 4
REDACTED = "<secret>"


def refs_in(tree: str) -> set[str]:
    """Every ref the tree carries."""
    return set(_REF_RE.findall(tree))


def ref_lines(tree: str) -> dict[str, str]:
    """``ref -> its (stripped) line``, in document order."""
    out: dict[str, str] = {}
    for line in tree.splitlines():
        m = _REF_RE.search(line)
        if m:
            out[m.group(1)] = line.strip()
    return out


def parse_line(line: str) -> dict[str, str | int] | None:
    """``{indent, role, name, ref}`` for a node line; None for a property or
    blank line. ``name`` is ``""`` when the node has none, ``ref`` likewise."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    ref = _REF_RE.search(line)
    return {
        "indent": len(m.group("indent")),
        "role": m.group("role"),
        "name": (m.group("name") or "").replace('\\"', '"'),
        "ref": ref.group(1) if ref else "",
    }


def interactive_only(tree: str) -> str:
    """Keep the lines that carry a ref (the nodes the agent can act on),
    with their indentation, so the structure still reads."""
    return "\n".join(line for line in tree.splitlines() if _REF_RE.search(line))


def truncate_at_line(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``max_chars`` on a LINE boundary. Returns
    ``(text, cut)``; the caller states the full size when ``cut``."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    return head, True


def redact_values(text: str, secrets: Iterable[str]) -> str:
    """Blank every occurrence of every secret value in ``text``.

    Used on everything that leaves the pod for the model — the tree, the
    page text, a script's result, a response body — so a password typed by
    ``login`` (or one the human typed in the viewer) never rides a tool result
    into the transcript. Secrets under :data:`MIN_SECRET_LEN` are skipped.
    """
    out = text
    for secret in sorted(
        {s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True
    ):
        out = out.replace(secret, REDACTED)
    return out


# Query words the literal tier reads as a ROLE rather than as text, so "search
# box" finds a `searchbox` whose name never says "box". Every other word must
# appear in the node's line or its ancestors' names.
_ROLE_WORDS: dict[str, frozenset[str]] = {
    "box": frozenset({"textbox", "searchbox", "combobox", "checkbox", "spinbutton"}),
    "field": frozenset({"textbox", "searchbox", "combobox", "spinbutton"}),
    "input": frozenset(
        {"textbox", "searchbox", "combobox", "spinbutton", "checkbox", "radio", "slider", "switch"}
    ),
    "search": frozenset({"searchbox", "search"}),
    "button": frozenset({"button"}),
    "link": frozenset({"link"}),
    "links": frozenset({"link"}),
    "dropdown": frozenset({"combobox", "listbox", "menu"}),
    "select": frozenset({"combobox", "listbox"}),
    "menu": frozenset({"menu", "menubar", "menuitem"}),
    "checkbox": frozenset({"checkbox"}),
    "radio": frozenset({"radio"}),
    "image": frozenset({"img"}),
    "picture": frozenset({"img"}),
    "img": frozenset({"img"}),
    "heading": frozenset({"heading"}),
    "title": frozenset({"heading"}),
    "tab": frozenset({"tab"}),
    "tabs": frozenset({"tab", "tablist"}),
    "row": frozenset({"row"}),
    "table": frozenset({"table", "grid"}),
    "dialog": frozenset({"dialog", "alertdialog"}),
    "form": frozenset({"form"}),
}
# Words that carry no target: dropped before matching, never a reason to miss.
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "for",
        "to",
        "at",
        "this",
        "that",
        "with",
        "and",
        "or",
        "page",
        "site",
        "element",
    }
)
_WORD_RE = re.compile(r"[a-z0-9#'&./-]+")


def literal_matches(tree: str, query: str, *, limit: int = 20) -> list[dict[str, object]]:
    """The literal tier of ``find``, two passes over the tree's own words:
    a case-insensitive regex (or plain text when the query is not a valid
    regex) over every line first; on a miss, every query word must be found
    in the node's line or its ancestors' names, with :data:`_ROLE_WORDS`
    ("box", "button", "link", …) matching the node's ROLE instead — the shape
    of the extension's queries ("search box", "sign in button").

    Each hit carries the node's ref (``""`` for a text node), its line, its
    ancestor path (``role "name" > role "name"``) and three lines of context
    either side. Capped at ``limit`` hits, the extension's own cap.
    """
    try:
        rx = re.compile(query, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(query), re.IGNORECASE)
    lines = tree.splitlines()
    hits: list[dict[str, object]] = []
    for i, line in enumerate(lines):
        if not rx.search(line):
            continue
        parsed = parse_line(line)
        hits.append(_hit(lines, i, (parsed or {}).get("ref", "") or "", _ancestor_path(lines, i)))
        if len(hits) >= limit:
            break
    return hits or _word_matches(lines, query, limit)


def _hit(lines: list[str], i: int, ref: object, path: str) -> dict[str, object]:
    return {
        "ref": ref,
        "line": lines[i].strip(),
        "path": path,
        "context": [ln.strip() for ln in lines[max(0, i - 3) : i + 4]],
    }


def _word_matches(lines: list[str], query: str, limit: int) -> list[dict[str, object]]:
    words = [w for w in _WORD_RE.findall(query.lower()) if w not in _STOP_WORDS]
    if not words:
        return []
    hits: list[dict[str, object]] = []
    stack: list[tuple[int, str]] = []  # (indent, `role "name"`) of the open ancestors
    for i, line in enumerate(lines):
        parsed = parse_line(line)
        if parsed is None:
            continue
        indent = int(parsed["indent"])
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = " > ".join(label for _, label in stack)
        role = str(parsed["role"]).lower()
        hay = line.lower()
        path_lower = path.lower()
        if all(w in hay or w in path_lower or role in _ROLE_WORDS.get(w, ()) for w in words):
            hits.append(_hit(lines, i, parsed["ref"] or "", path))
            if len(hits) >= limit:
                break
        name = str(parsed["name"])
        stack.append((indent, str(parsed["role"]) + (f' "{name}"' if name else "")))
    return hits


def _ancestor_path(lines: list[str], index: int) -> str:
    parsed = parse_line(lines[index])
    if parsed is None:
        return ""
    depth = int(parsed["indent"])
    chain: list[str] = []
    for j in range(index - 1, -1, -1):
        p = parse_line(lines[j])
        if p is None or int(p["indent"]) >= depth:
            continue
        depth = int(p["indent"])
        label = str(p["role"]) + (f' "{p["name"]}"' if p["name"] else "")
        chain.append(label)
        if depth == 0:
            break
    return " > ".join(reversed(chain))


def format_matches(hits: list[dict[str, object]], *, source: str) -> str:
    """The ``find`` reply: one hit per block, ref first so the model can act
    on it, path and context below."""
    if not hits:
        return "No match."
    blocks: list[str] = [f"{len(hits)} match(es), source: {source}"]
    for hit in hits:
        ref = str(hit.get("ref") or "")
        head = f"{ref}: {hit['line']}" if ref else f"(no ref) {hit['line']}"
        if hit.get("reason"):
            head += f" — {hit['reason']}"
        path = str(hit.get("path") or "")
        lines = [head]
        if path:
            lines.append(f"  in: {path}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
