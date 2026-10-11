"""The reference documentation is the docstrings: these tests keep it whole.

Sphinx builds the API pages from the docstrings and ``help()`` shows them as
they are, so a public method without a docstring, a parameter missing from
``Args:`` or an example that stopped working is a bug like any other.
"""

from __future__ import annotations

import dataclasses
import doctest
import enum
import functools
import importlib
import inspect
import pkgutil
import re
import sys
from typing import Any

import pytest

import pgpcbot
from pgpcbot import errors, filters, router, webhook

# public, documented under their own module because the package does not re-export them
EXTRA: list[Any] = [
    filters.FilterContext,
    router.Handler,
    router.ErrorHandler,
    webhook.WebhookResult,
    errors.error_from_envelope,
]

SECTION = re.compile(r"(Args|Returns|Yields|Raises|Attributes|Example|Examples|Note|Warning|See Also):")
ENTRY = re.compile(r"(\*{0,2}[A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:")
NOTHING = {"None", "NoReturn", "Never"}


def sections(doc: str | None) -> dict[str, list[str]]:
    """The Google-style sections of a docstring, each with its raw lines."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in inspect.cleandoc(doc or "").splitlines():
        header = SECTION.fullmatch(line)
        if header:
            current = found.setdefault(header.group(1), [])
        elif current is not None and line and not line[0].isspace():
            current = None
        elif current is not None:
            current.append(line)
    return found


def entries(lines: list[str]) -> list[str]:
    """The names an ``Args:`` or ``Attributes:`` section describes, stars dropped."""
    depths = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    if not depths:
        return []
    names = []
    for line in lines:
        if line.strip() and len(line) - len(line.lstrip()) == min(depths):
            found = ENTRY.match(line.strip())
            if found:
                names.append(found.group(1).lstrip("*"))
    return names


def ours(obj: Any) -> bool:
    return getattr(obj, "__module__", "").startswith("pgpcbot")


def own_doc(obj: Any) -> str | None:
    if inspect.isclass(obj):
        # a class without a docstring would show its parent's in help(); that does not count
        return vars(obj).get("__doc__")
    if getattr(obj, "__name__", "") == "__init__":
        return obj.__doc__  # type: ignore[no-any-return]
    # an override without a docstring shows its parent's, in help() and in Sphinx alike
    return inspect.getdoc(obj)


def written_init(cls: type) -> tuple[type, Any] | None:
    """The ``__init__`` a class runs, when it is ours and written by hand."""
    for owner in cls.__mro__:
        if "__init__" in vars(owner):
            init = vars(owner)["__init__"]
            module_file = getattr(sys.modules.get(owner.__module__), "__file__", None)
            if ours(owner) and inspect.isfunction(init) and init.__code__.co_filename == module_file:
                return owner, init
            return None
    return None


def public_objects() -> dict[str, Any]:
    found: dict[str, Any] = {}
    seen: set[int] = set()

    def add(label: str, obj: Any) -> None:
        if id(obj) not in seen:
            seen.add(id(obj))
            found[label] = obj

    for name in pgpcbot.__all__:
        obj = getattr(pgpcbot, name)
        if inspect.isclass(obj) or inspect.isfunction(obj):
            add(name, obj)
    for name in filters.__all__:
        obj = getattr(filters, name)
        # the ready-made filters are values: their docs live in the source, for Sphinx
        if inspect.isclass(obj) or inspect.isfunction(obj):
            add(f"filters.{name}", obj)
    for obj in EXTRA:
        add(f"{obj.__module__.removeprefix('pgpcbot.')}.{obj.__qualname__}", obj)
    return found


def members(cls: type) -> dict[str, Any]:
    """Public methods and properties, each under the class that defines it."""
    found = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        owner = next((klass for klass in cls.__mro__ if name in vars(klass)), None)
        if owner is None or not ours(owner):
            continue
        value = vars(owner)[name]
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, (property, functools.cached_property)) or inspect.isfunction(value):
            found[f"{owner.__qualname__}.{name}"] = value
    return found


def collect() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    documented: dict[str, Any] = {}
    callables: dict[str, Any] = {}
    records: dict[str, Any] = {}
    for label, obj in public_objects().items():
        documented[label] = obj
        if inspect.isfunction(obj):
            callables[label] = obj
            continue
        if dataclasses.is_dataclass(obj) or issubclass(obj, enum.Enum):
            records[label] = obj
        init = written_init(obj)
        if init is not None:
            owner, func = init
            documented.setdefault(f"{owner.__qualname__}.__init__", func)
            callables.setdefault(f"{owner.__qualname__}.__init__", func)
        for key, value in members(obj).items():
            documented.setdefault(key, value)
            if inspect.isfunction(value):
                callables.setdefault(key, value)
    return documented, callables, records


DOCUMENTED, CALLABLES, RECORDS = collect()
MODULES = sorted(m.name for m in pkgutil.walk_packages(pgpcbot.__path__, "pgpcbot."))


def test_the_lists_are_not_empty() -> None:
    # a refactor that breaks collect() must not turn every check below into a pass
    assert len(DOCUMENTED) > 300
    assert len(CALLABLES) > 150
    assert len(RECORDS) > 40


@pytest.mark.parametrize("module", ["pgpcbot", *MODULES])
def test_every_module_says_what_it_is(module: str) -> None:
    if module == "pgpcbot._version":
        pytest.skip("one line, written by the release")
    doc = importlib.import_module(module).__doc__
    assert (doc or "").strip(), f"{module} has no module docstring"


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_everything_public_has_a_docstring(name: str) -> None:
    obj = DOCUMENTED[name]
    doc = own_doc(obj) or ""
    assert doc.strip(), f"{name} has no docstring"
    if inspect.isclass(obj) and dataclasses.is_dataclass(obj):
        assert not doc.startswith(f"{obj.__name__}("), f"{name} only has the docstring dataclass writes for it"


@pytest.mark.parametrize("name", sorted(CALLABLES))
def test_every_parameter_is_described(name: str) -> None:
    func = CALLABLES[name]
    wanted = [p for p in inspect.signature(func).parameters if p not in ("self", "cls")]
    described = entries(sections(own_doc(func)).get("Args", []))
    missing = [p for p in wanted if p not in described]
    extra = [p for p in described if p not in wanted]
    assert (missing, extra) == ([], []), f"{name}: Args misses {missing}, describes unknown {extra}"


@pytest.mark.parametrize("name", sorted(n for n in CALLABLES if not n.endswith(".__init__")))
def test_what_comes_back_is_described(name: str) -> None:
    func = CALLABLES[name]
    returns = inspect.signature(func).return_annotation
    if returns is inspect.Signature.empty or str(returns) in NOTHING:
        return
    found = sections(own_doc(func))
    assert "Returns" in found or "Yields" in found, f"{name} returns {returns} but has no Returns: section"


@pytest.mark.parametrize("name", sorted(RECORDS))
def test_every_field_is_described(name: str) -> None:
    cls = RECORDS[name]
    if issubclass(cls, enum.Enum):
        wanted = [member.name for member in cls]
    else:
        wanted = [f.name for f in dataclasses.fields(cls) if not f.name.startswith("_") and f.name != "raw"]
    described: list[str] = []
    for klass in cls.__mro__:
        # a field is described where it is declared, an inherited one in the parent
        if ours(klass):
            described += entries(sections(own_doc(klass)).get("Attributes", []))
    missing = [name for name in wanted if name not in described]
    assert not missing, f"{name}: Attributes misses {missing}"


@pytest.mark.parametrize("name", sorted(RECORDS))
def test_no_field_is_described_that_does_not_exist(name: str) -> None:
    cls = RECORDS[name]
    described = entries(sections(own_doc(cls)).get("Attributes", []))
    real = {member.name for member in cls} if issubclass(cls, enum.Enum) else {f.name for f in dataclasses.fields(cls)}
    unknown = [entry for entry in described if entry not in real and not hasattr(cls, entry)]
    assert not unknown, f"{name}: Attributes describes {unknown}, which {cls.__name__} does not have"


@pytest.mark.parametrize("module", MODULES)
def test_examples_in_docstrings_run(module: str) -> None:
    flags = doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE
    result = doctest.testmod(importlib.import_module(module), optionflags=flags, report=True)
    assert result.failed == 0, f"{result.failed} example(s) in {module} gave another result, see above"
