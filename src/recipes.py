"""Saved multi-step browser recipes — parsing, targeting, and what may run.

A *recipe* is a click-path a tenant has already proven works on a site, stored
as data so it can be executed **without a model in the loop**. Benchmarked on a
live site in 2026-09: the same task takes 9 model calls when a model decides
each step, 8 when it is handed a prose skill describing the steps, and 1 when
the steps are executed as data — roughly a 20x cost difference. The lever is not
how well the steps are described; it is whether a model is asked between them.

Pure module by design: no driver, no IO, no MCP. Everything here is a function
over plain data so the targeting and the safety rules are unit-testable without
a browser.

**Refs cannot be stored.** A snapshot ref (``e12``) is a handle into one page
render and means nothing on a later run, so a recipe names each control by a
*descriptor* (tag / type / accessible name) and :func:`resolve_target` re-finds
it against a fresh snapshot.

**A recipe cannot REFERENCE a stored credential.** There is no ``credential:``
value source, on purpose: a login step calls the existing ``browser_login`` tool,
which injects the tenant's portal password server-side and keeps it out of the
agent's context and off this file. That is the guarantee — it is NOT a guarantee
that nobody can hand-type a literal password into a ``browser_type`` step, which
:func:`parse` cannot tell from any other text. The narrow claim is the true one;
do not restate it as "recipes never carry secrets".
"""

from __future__ import annotations

from typing import Any, Final, Literal, TypedDict

# What a recipe is allowed to execute. An ALLOWLIST rather than a denylist, and
# not configurable: a recipe is data that arrives on the tenant volume, so
# anything it can call is reachable by whoever can write a file there.
# `browser_eval` (arbitrary JS), `browser_upload_file` and `browser_download`
# (filesystem reach) are deliberately absent — a recipe navigates and reads, it
# does not execute code or move files.
ALLOWED_TOOLS = frozenset(
    {
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_press_key",
        "browser_scroll",
        "browser_wait_for",
        "browser_read",
        "browser_get_table",
        "browser_get_links",
        "browser_find",
        "browser_login",
        "browser_back",
        "browser_forward",
        "browser_reload",
    }
)

# Steps whose output is the CONTENT a caller wants back, as opposed to
# navigation. Only these are returned, so a recipe's result is the answer rather
# than a transcript of everything it touched.
EXTRACTION_TOOLS = frozenset(
    {"browser_read", "browser_get_table", "browser_get_links", "browser_find"}
)

_PARAM_PREFIX = "param:"

# The ONLY keys a descriptor may carry. An unknown key used to be ignored, and
# ignoring one is far worse than rejecting it here: a target of
# ``{"role": "button"}`` reduced to "match on nothing", which EVERY element
# satisfies, so the recipe clicked an arbitrary control and reported success.
_TARGET_KEYS = frozenset({"tag", "type", "name", "nth"})
# Typed as literals, not plain str, so mypy can still index the Target TypedDict
# with them.
_MatchKey = Literal["tag", "type", "name"]
_MATCH_KEYS: Final[tuple[_MatchKey, ...]] = ("tag", "type", "name")


class RecipeError(ValueError):
    """A recipe is malformed, unsafe, or cannot be applied to this page.

    One exception type on purpose: every case here is "the caller must fix the
    recipe or the page changed", which the tool maps to one honest status.
    """


class Target(TypedDict, total=False):
    """How to re-find one control on a later page render."""

    tag: str
    type: str
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
        if not isinstance(step, dict):
            raise RecipeError(f"step {index} must be an object")
        tool = step.get("tool")
        if not isinstance(tool, str):
            raise RecipeError(f"step {index} has no 'tool'")
        if tool not in ALLOWED_TOOLS:
            raise RecipeError(
                f"step {index}: '{tool}' is not allowed in a recipe "
                f"(allowed: {', '.join(sorted(ALLOWED_TOOLS))})"
            )

        args = step.get("args", {})
        if not isinstance(args, dict):
            raise RecipeError(f"step {index}: 'args' must be an object")
        # A ref is a handle into ONE page render (see the module docstring). It
        # is exactly the shape you get by pasting a working tool call into a
        # recipe, and it skips resolve_target entirely — on a later run it
        # matches nothing, or worse, whatever now happens to carry that ref.
        if "ref" in args:
            raise RecipeError(
                f"step {index}: a recipe cannot store 'ref' — a ref is valid for one page "
                "render only; use 'target' with tag/type/name instead"
            )

        if "target" in step:
            _check_target(step["target"], f"step {index}")

        if tool == "browser_fill_form":
            if "fields" in args:
                raise RecipeError(f"step {index}: put 'fields' on the step, not inside 'args'")
            fields = step.get("fields")
            # Absent or empty used to fill nothing and still report success — a
            # blank form submitted to a live site by a recipe that reads fine.
            if not isinstance(fields, list) or not fields:
                raise RecipeError(f"step {index}: browser_fill_form needs a non-empty 'fields'")
            for position, field in enumerate(fields):
                if not isinstance(field, dict) or "target" not in field:
                    raise RecipeError(f"step {index}: every field needs a 'target'")
                _check_target(field["target"], f"step {index} field {position}")
        elif step.get("fields"):
            raise RecipeError(f"step {index}: only browser_fill_form takes 'fields'")

    return {
        "name": str(raw.get("name", "")),
        "params": list(declared),
        "steps": steps,
    }


def _check_target(target: Any, where: str) -> None:
    """Reject a descriptor that cannot mean exactly one thing.

    Split out because a fill_form field carries a target too, and the two used
    to be validated differently."""
    if not isinstance(target, dict):
        raise RecipeError(f"{where}: 'target' must be an object")
    unknown = set(target) - _TARGET_KEYS
    if unknown:
        raise RecipeError(
            f"{where}: unknown target key(s) {sorted(unknown)} — "
            f"a target may only use {sorted(_TARGET_KEYS)}"
        )
    if not any(key in target for key in _MATCH_KEYS):
        raise RecipeError(f"{where}: a target needs at least one of {list(_MATCH_KEYS)}")
    if "nth" in target:
        _check_nth(target["nth"], where)


def _check_nth(nth: Any, where: str) -> None:
    """``isinstance(True, int)`` is True in Python, so bools need their own gate.

    ``"nth": true`` otherwise reads as index 1 and silently picks the second
    match."""
    if isinstance(nth, bool) or not isinstance(nth, int) or nth < 0:
        raise RecipeError(f"{where}: 'nth' must be a non-negative integer, got {nth!r}")


def missing_params(recipe: dict[str, Any], params: dict[str, str]) -> list[str]:
    """Declared parameters the caller did not supply.

    Checked up front so a recipe fails before it touches the site, rather than
    half-way through with a form partly filled."""
    return [name for name in recipe["params"] if name not in params]


def substitute(value: Any, params: dict[str, str]) -> Any:
    """Resolve a recipe value: ``param:<name>`` from the caller, else literal.

    Unknown parameters raise instead of rendering empty — typing "" into a
    portal's search box returns every row, which reads like a working run."""
    if not isinstance(value, str) or not value.startswith(_PARAM_PREFIX):
        return value
    name = value[len(_PARAM_PREFIX) :]
    if name not in params:
        raise RecipeError(f"recipe needs parameter '{name}', which was not supplied")
    return params[name]


def resolve_target(elements: list[dict[str, Any]], target: Target) -> str:
    """The CURRENT ref for a stored descriptor, against a fresh snapshot.

    Ambiguity raises unless the recipe disambiguates with ``nth``. Quietly
    taking the first of several matches is how a replay clicks the wrong button
    and still reports success — the failure mode these experiments exist to
    catch."""
    wanted = {k: str(target[k]).strip() for k in _MATCH_KEYS if k in target}
    if not wanted:
        raise RecipeError(f"target {dict(target)!r} names no tag/type/name to match on")
    matches = [
        el
        for el in elements
        if all(str(el.get(key, "")).strip() == value for key, value in wanted.items())
    ]
    if not matches:
        raise RecipeError(f"no element matches {wanted} on this page ({len(elements)} seen)")

    nth = target.get("nth")
    if nth is None:
        if len(matches) > 1:
            raise RecipeError(f"{len(matches)} elements match {wanted}; add 'nth' to say which")
        return str(matches[0]["ref"])

    _check_nth(nth, "target")
    if nth >= len(matches):
        raise RecipeError(f"nth={nth} out of range for {wanted} ({len(matches)} matches)")
    return str(matches[nth]["ref"])
