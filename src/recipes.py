"""Saved multi-step browser recipes — parsing, targeting, and what may run.

A *recipe* is a click-path someone has already proven works on a site, stored
as data so it can be executed **without a model in the loop**. Benchmarked on
a live site in 2026-09: the same task takes 9 model calls when a model decides
each step, 8 when it is handed a prose skill describing the steps, and 1 when
the steps are executed as data — roughly a 20x cost difference. The lever is
not how well the steps are described; it is whether a model is asked between
them.

A recipe is a **stored batch**: each step is ``{name, input}`` exactly as a
``browser_batch`` item, plus an optional ``target`` in place of a ref.

**Refs cannot be stored.** A ref (``e12``) is a handle into one page render
and means nothing on a later run, so a recipe names each control by a
*descriptor* — the node's accessibility ``role`` and ``name`` as ``read_page``
prints them — and :func:`resolve_target` re-finds it against the tree the
recipe's own preceding ``read_page`` step produced.

**A recipe cannot REFERENCE a stored credential.** There is no ``credential:``
value source, on purpose: a login step calls the ``login`` tool, which injects
the stored password below the agent and keeps it off this file. That is the
guarantee — it is NOT a guarantee that nobody can hand-type a literal password
into a ``computer`` ``type`` step, which :func:`parse` cannot tell from any
other text. The narrow claim is the true one; do not restate it as "recipes
never carry secrets".

Pure module by design: no driver, no IO, no MCP.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from src.refs_tree import parse_line

# What a recipe is allowed to execute. An ALLOWLIST rather than a denylist, and
# not configurable: a recipe is data that arrives on the data volume, so
# anything it can call is reachable by whoever can write a file there.
# `javascript_tool` (arbitrary JS), `file_upload` and `download` (filesystem
# reach) are deliberately absent — a recipe navigates and reads, it does not
# execute code or move files.
ALLOWED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "navigate",
        "computer",
        "read_page",
        "get_page_text",
        "find",
        "form_input",
        "wait_for",
        "login",
    }
)

# The `computer` actions a recipe may take: the ones that drive a click-path.
# No `screenshot`/`zoom` (context bloat with no model to look at it) and no
# drag or hover (never a proven path).
COMPUTER_ACTIONS: Final[frozenset[str]] = frozenset(
    {"left_click", "type", "key", "scroll", "wait", "scroll_to"}
)

# Steps whose output is the CONTENT a caller wants back, as opposed to
# navigation. Only these are returned, so a recipe's result is the answer
# rather than a transcript of everything it touched.
EXTRACTION_TOOLS: Final[frozenset[str]] = frozenset({"read_page", "get_page_text", "find"})

# Tools that take a `ref` and may therefore carry a `target` instead.
_REF_TOOLS: Final[frozenset[str]] = frozenset({"computer", "form_input", "login"})

_PARAM_PREFIX = "param:"

# The ONLY keys a descriptor may carry. An unknown key used to be ignored, and
# ignoring one is far worse than rejecting it: a target of ``{"kind": "button"}``
# reduced to "match on nothing", which EVERY node satisfies.
_TARGET_KEYS: Final[frozenset[str]] = frozenset({"role", "name", "nth"})


class RecipeError(ValueError):
    """A recipe is malformed, unsafe, or cannot be applied to this page.

    One exception type on purpose: every case here is "the caller must fix the
    recipe or the page changed", which the tool maps to one honest status."""


class Target(TypedDict, total=False):
    """How to re-find one control on a later page render: the accessibility
    role and name ``read_page`` prints, plus ``nth`` when several match."""

    role: str
    name: str
    nth: int


def parse(raw: Any) -> dict[str, Any]:
    """Validate a recipe document and return it normalised.

    Rejects rather than repairs. A recipe that half-parses would execute a
    partial click-path against a live site, which is worse than not running."""
    if not isinstance(raw, dict):
        raise RecipeError(f"recipe must be an object, got {type(raw).__name__}")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RecipeError("recipe needs a non-empty 'steps' list")

    declared = raw.get("params", [])
    if not isinstance(declared, list) or any(not isinstance(p, str) for p in declared):
        raise RecipeError("'params' must be a list of names")

    for index, step in enumerate(steps):
        _check_step(step, index)

    return {
        "name": str(raw.get("name", "")),
        "params": list(declared),
        "steps": steps,
    }


def _check_step(step: Any, index: int) -> None:
    where = f"step {index}"
    if not isinstance(step, dict):
        raise RecipeError(f"{where} must be an object")
    name = step.get("name")
    if not isinstance(name, str):
        raise RecipeError(f"{where} has no 'name'")
    if name not in ALLOWED_TOOLS:
        raise RecipeError(
            f"{where}: '{name}' is not allowed in a recipe "
            f"(allowed: {', '.join(sorted(ALLOWED_TOOLS))})"
        )
    inp = step.get("input", {})
    if not isinstance(inp, dict):
        raise RecipeError(f"{where}: 'input' must be an object")
    # A ref is a handle into ONE page render (see the module docstring). It is
    # exactly the shape you get by pasting a working tool call into a recipe,
    # and it skips resolve_target entirely — on a later run it matches nothing,
    # or worse, whatever now happens to carry that ref.
    if "ref" in inp:
        raise RecipeError(
            f"{where}: a recipe cannot store 'ref' — a ref is valid for one page "
            "render only; use 'target' with role/name instead"
        )
    if name == "computer":
        action = inp.get("action")
        if action not in COMPUTER_ACTIONS:
            raise RecipeError(
                f"{where}: computer action {action!r} is not allowed in a recipe "
                f"(allowed: {', '.join(sorted(COMPUTER_ACTIONS))})"
            )
    if "target" in step:
        if name not in _REF_TOOLS:
            raise RecipeError(f"{where}: '{name}' takes no ref, so it takes no 'target'")
        _check_target(step["target"], where)


def _check_target(target: Any, where: str) -> None:
    """Reject a descriptor that cannot mean exactly one thing."""
    if not isinstance(target, dict):
        raise RecipeError(f"{where}: 'target' must be an object")
    unknown = set(target) - _TARGET_KEYS
    if unknown:
        raise RecipeError(
            f"{where}: unknown target key(s) {sorted(unknown)} — "
            f"a target may only use {sorted(_TARGET_KEYS)}"
        )
    if not any(key in target for key in ("role", "name")):
        raise RecipeError(f"{where}: a target needs 'role' and/or 'name'")
    if "nth" in target:
        _check_nth(target["nth"], where)


def _check_nth(nth: Any, where: str) -> None:
    """``isinstance(True, int)`` is True in Python, so bools need their own
    gate: ``"nth": true`` otherwise reads as index 1."""
    if isinstance(nth, bool) or not isinstance(nth, int) or nth < 0:
        raise RecipeError(f"{where}: 'nth' must be a non-negative integer, got {nth!r}")


def missing_params(recipe: dict[str, Any], params: dict[str, str]) -> list[str]:
    """Declared parameters the caller did not supply — checked up front so a
    recipe fails before it touches the site, not half-way through a form."""
    return [name for name in recipe["params"] if name not in params]


def substitute(value: Any, params: dict[str, str]) -> Any:
    """Resolve ``param:<name>`` values from the caller, recursively through
    an input object; everything else is literal.

    Unknown parameters raise instead of rendering empty — typing "" into a
    portal's search box returns every row, which reads like a working run."""
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    if not isinstance(value, str) or not value.startswith(_PARAM_PREFIX):
        return value
    name = value[len(_PARAM_PREFIX) :]
    if name not in params:
        raise RecipeError(f"recipe needs parameter '{name}', which was not supplied")
    return params[name]


def resolve_target(tree: str, target: Target) -> str:
    """The CURRENT ref for a stored descriptor, against a fresh ``read_page``.

    Ambiguity raises unless the recipe disambiguates with ``nth``. Quietly
    taking the first of several matches is how a replay clicks the wrong
    button and still reports success."""
    wanted_role = str(target.get("role", "")).strip()
    wanted_name = str(target.get("name", "")).strip() if "name" in target else None
    if not wanted_role and wanted_name is None:
        raise RecipeError(f"target {dict(target)!r} names no role/name to match on")
    matches: list[str] = []
    seen = 0
    for line in tree.splitlines():
        node = parse_line(line)
        if node is None or not node["ref"]:
            continue
        seen += 1
        if wanted_role and str(node["role"]) != wanted_role:
            continue
        if wanted_name is not None and str(node["name"]).strip() != wanted_name:
            continue
        matches.append(str(node["ref"]))
    wanted = {k: v for k, v in (("role", wanted_role), ("name", wanted_name)) if v}
    if not matches:
        raise RecipeError(f"no element matches {wanted} on this page ({seen} nodes with refs)")

    nth = target.get("nth")
    if nth is None:
        if len(matches) > 1:
            raise RecipeError(f"{len(matches)} elements match {wanted}; add 'nth' to say which")
        return matches[0]
    _check_nth(nth, "target")
    if nth >= len(matches):
        raise RecipeError(f"nth={nth} out of range for {wanted} ({len(matches)} matches)")
    return matches[nth]
