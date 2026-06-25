# pyright: strict

"""Documentation pipeline skeleton for FreeCAD Python binding APIs.

This module deliberately implements a small tracer-bullet flow:
- normalize discovered binding registrations into a versioned JSON model
- derive an agent-oriented API index from that model
- render a disposable Sphinx RST subtree from the normalized JSON
- validate the minimal JSON schemas used by this first slice
- summarize documentation completeness for quality reporting

The inputs are the same source-discovery records used by stub generation, so
normal documentation generation does not import or require a built FreeCAD
runtime.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

from .discovery import group_methods
from .model import BindingMethod

NORMALIZED_SCHEMA_VERSION = "freecad-doc-normalized-v1"
AGENT_INDEX_SCHEMA_VERSION = "freecad-agent-api-index-v1"

NORMALIZED_JSON_NAME = "normalized-documentation.json"
AGENT_INDEX_JSON_NAME = "agent-api-index.json"
QUALITY_REPORT_NAME = "documentation-quality-report.md"
SPHINX_SUBTREE = Path("sphinx") / "python_api"

CompletenessState = str


class DocumentationSchemaError(ValueError):
    """Raised when a generated documentation JSON file violates its schema."""


def completeness_for_doc(doc: str) -> CompletenessState:
    return "documented" if doc.strip() else "missing"


def method_summary(method: BindingMethod) -> str:
    return method.doc.splitlines()[0].strip() if method.doc else ""


def method_signature(method: BindingMethod) -> str:
    match method.method_kind:
        case "noargs":
            return "()"
        case "keyword":
            return "(*args, **kwargs)"
        case _:
            return "(*args)"


def normalized_model(methods: list[BindingMethod]) -> dict[str, Any]:
    module_methods, type_methods, unknown_methods = group_methods(methods)
    modules: dict[str, list[BindingMethod]] = defaultdict(list)

    for module_name, group in module_methods.items():
        modules[module_name].extend(group)
    for type_name, group in type_methods.items():
        modules[f"<type-context>.{type_name}"].extend(group)
    for context_name, group in unknown_methods.items():
        modules[f"<unknown>.{context_name}"].extend(group)

    normalized_modules: list[dict[str, Any]] = []
    for module_name in sorted(modules):
        members = [
            {
                "name": method.python_name,
                "qualified_name": f"{module_name}.{method.python_name}",
                "kind": "function",
                "signature": method_signature(method),
                "summary": method_summary(method),
                "doc": method.doc,
                "completeness": completeness_for_doc(method.doc),
                "source": {
                    "path": method.source,
                    "line": method.line,
                },
            }
            for method in sorted(modules[module_name], key=lambda item: (item.python_name, item.line))
        ]
        counts = Counter(member["completeness"] for member in members)
        normalized_modules.append(
            {
                "name": module_name,
                "summary": "",
                "completeness": "missing" if counts.get("documented", 0) == 0 else "partial",
                "members": members,
            }
        )

    return {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "metadata": {
            "generator": "src/Tools/typing/generate_stubs.py generate-docs",
            "source": "FreeCAD Python binding discovery",
        },
        "modules": normalized_modules,
    }


def agent_api_index(model: dict[str, Any]) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    for module in model.get("modules", []):
        for member in module.get("members", []):
            symbols.append(
                {
                    "qualified_name": member["qualified_name"],
                    "kind": member["kind"],
                    "signature": member["signature"],
                    "summary": member["summary"],
                    "completeness": member["completeness"],
                    "source": member["source"],
                }
            )

    return {
        "schema_version": AGENT_INDEX_SCHEMA_VERSION,
        "source_schema_version": model["schema_version"],
        "symbols": sorted(symbols, key=lambda symbol: symbol["qualified_name"]),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rst_anchor(name: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in name).strip("-")


def rst_heading(title: str, underline: str) -> list[str]:
    return [title, underline * len(title), ""]


def render_sphinx_rst(model: dict[str, Any], out_dir: Path) -> list[Path]:
    sphinx_dir = out_dir / SPHINX_SUBTREE
    sphinx_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    module_pages: list[tuple[str, str]] = []
    for module in model["modules"]:
        module_name = module["name"]
        filename = f"{rst_anchor(module_name) or 'unknown'}.rst"
        module_pages.append((module_name, filename))
        lines = rst_heading(module_name, "=")
        lines.extend([".. This file is generated from normalized-documentation.json.", ""])

        members = module.get("members", [])
        if not members:
            lines.extend(["No Python API members were discovered for this module.", ""])
        for member in members:
            lines.extend(rst_heading(member["name"], "-"))
            lines.append(f"``{member['qualified_name']}{member['signature']}``")
            lines.append("")
            lines.append(f"Completeness: ``{member['completeness']}``")
            lines.append("")
            if member["summary"]:
                lines.append(member["summary"])
                lines.append("")
            source = member["source"]
            lines.append(f"Source: ``{source['path']}:{source['line']}``")
            lines.append("")

        path = sphinx_dir / filename
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(path)

    index_lines = rst_heading("FreeCAD Python API", "=")
    index_lines.extend(
        [
            ".. This subtree is disposable generated documentation.",
            "",
            ".. toctree::",
            "   :maxdepth: 2",
            "",
        ]
    )
    for _, filename in module_pages:
        index_lines.append(f"   {filename.removesuffix('.rst')}")
    index_lines.append("")

    index_path = sphinx_dir / "index.rst"
    index_path.write_text("\n".join(index_lines), encoding="utf-8")
    written.append(index_path)
    return written


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DocumentationSchemaError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DocumentationSchemaError(f"{path}: top-level JSON value must be an object")
    return payload


def require_string(container: dict[str, Any], key: str, location: str) -> None:
    if not isinstance(container.get(key), str):
        raise DocumentationSchemaError(f"{location}.{key} must be a string")


def require_object(container: dict[str, Any], key: str, location: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise DocumentationSchemaError(f"{location}.{key} must be an object")
    return value


def validate_normalized_model(model: dict[str, Any]) -> None:
    if model.get("schema_version") != NORMALIZED_SCHEMA_VERSION:
        raise DocumentationSchemaError(
            f"schema_version must be {NORMALIZED_SCHEMA_VERSION!r}"
        )
    modules = model.get("modules")
    if not isinstance(modules, list):
        raise DocumentationSchemaError("modules must be a list")
    for module_index, module in enumerate(modules):
        location = f"modules[{module_index}]"
        if not isinstance(module, dict):
            raise DocumentationSchemaError(f"{location} must be an object")
        require_string(module, "name", location)
        require_string(module, "completeness", location)
        members = module.get("members")
        if not isinstance(members, list):
            raise DocumentationSchemaError(f"{location}.members must be a list")
        for member_index, member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            if not isinstance(member, dict):
                raise DocumentationSchemaError(f"{member_location} must be an object")
            for key in ("name", "qualified_name", "kind", "signature", "completeness"):
                require_string(member, key, member_location)
            require_object(member, "source", member_location)


def validate_agent_api_index(index: dict[str, Any]) -> None:
    if index.get("schema_version") != AGENT_INDEX_SCHEMA_VERSION:
        raise DocumentationSchemaError(
            f"schema_version must be {AGENT_INDEX_SCHEMA_VERSION!r}"
        )
    require_string(index, "source_schema_version", "$")
    symbols = index.get("symbols")
    if not isinstance(symbols, list):
        raise DocumentationSchemaError("symbols must be a list")
    for symbol_index, symbol in enumerate(symbols):
        location = f"symbols[{symbol_index}]"
        if not isinstance(symbol, dict):
            raise DocumentationSchemaError(f"{location} must be an object")
        for key in ("qualified_name", "kind", "signature", "completeness"):
            require_string(symbol, key, location)
        require_object(symbol, "source", location)


def validate_documentation_json(payload: dict[str, Any]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version == NORMALIZED_SCHEMA_VERSION:
        validate_normalized_model(payload)
        return
    if schema_version == AGENT_INDEX_SCHEMA_VERSION:
        validate_agent_api_index(payload)
        return
    raise DocumentationSchemaError(f"unknown schema_version: {schema_version!r}")


def completeness_counts(model: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for module in model.get("modules", []):
        for member in module.get("members", []):
            counts[member.get("completeness", "unknown")] += 1
    return counts


def quality_report(model: dict[str, Any]) -> str:
    validate_normalized_model(model)
    counts = completeness_counts(model)
    total = sum(counts.values())

    lines = [
        "# Documentation Quality Report",
        "",
        f"Schema version: `{model['schema_version']}`",
        f"Modules: {len(model['modules'])}",
        f"Members: {total}",
        "",
        "## Completeness State Counts",
        "",
    ]
    if counts:
        for state, count in sorted(counts.items()):
            lines.append(f"- `{state}`: {count}")
    else:
        lines.append("- `none`: 0")
    lines.append("")
    return "\n".join(lines)

