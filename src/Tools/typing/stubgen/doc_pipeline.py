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

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .discovery import group_methods
from .model import BindingMethod

NORMALIZED_SCHEMA_VERSION = "freecad-doc-normalized-v1"
AGENT_INDEX_SCHEMA_VERSION = "freecad-agent-api-index-v1"

NORMALIZED_JSON_NAME = "normalized-documentation.json"
AGENT_INDEX_JSON_NAME = "agent-api-index.json"
QUALITY_REPORT_NAME = "documentation-quality-report.md"
SPHINX_SUBTREE = Path("sphinx") / "python_api"
DOCUMENT_API_RST_NAME = "document-api-tracer-path.rst"
DOCUMENT_API_CATEGORIES = (
    "modules",
    "symbols",
    "objects",
    "properties",
    "examples",
    "anti_patterns",
)

CompletenessState = str
VALID_COMPLETENESS_STATES = frozenset(
    {"documented", "missing", "typed", "discovered", "partial"}
)


class DocumentationSchemaError(ValueError):
    """Raised when a generated documentation JSON file violates its schema."""


@dataclass(frozen=True)
class DocumentationValidationResult:
    completeness_gaps: tuple[str, ...] = ()
    availability_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StubSymbol:
    module_name: str
    name: str
    qualified_name: str
    kind: str
    signature: str
    summary: str
    completeness: CompletenessState
    source_path: str
    source_line: int


@dataclass(frozen=True)
class StubModule:
    name: str
    source_path: str
    source_line: int
    symbols: tuple[StubSymbol, ...]


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


def pyi_module_name(stubs_dir: Path, path: Path) -> str:
    relative = path.relative_to(stubs_dir)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def function_has_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    arguments: list[ast.arg] = (
        list(node.args.posonlyargs)
        + list(node.args.args)
        + list(node.args.kwonlyargs)
    )
    if node.args.vararg:
        arguments.append(node.args.vararg)
    if node.args.kwarg:
        arguments.append(node.args.kwarg)
    return node.returns is not None or any(
        argument.annotation is not None for argument in arguments
    )


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    text = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"({text}){returns}"


def class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases.extend(f"{keyword.arg}={ast.unparse(keyword.value)}" for keyword in node.keywords)
    return f"({', '.join(bases)})" if bases else ""


def stub_completeness(node: ast.AST) -> CompletenessState:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "typed" if function_has_annotations(node) else "discovered"
    return "discovered"


def doc_summary(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True)
    return doc.splitlines()[0].strip() if doc else ""


def public_stub_symbol(
    *,
    module_name: str,
    source_path: str,
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    parent_class: str | None = None,
) -> StubSymbol:
    if isinstance(node, ast.ClassDef):
        kind = "class"
        signature = class_signature(node)
    else:
        kind = "method" if parent_class else "function"
        signature = function_signature(node)
    name = f"{parent_class}.{node.name}" if parent_class else node.name
    return StubSymbol(
        module_name=module_name,
        name=name,
        qualified_name=f"{module_name}.{name}",
        kind=kind,
        signature=signature,
        summary=doc_summary(node),
        completeness=stub_completeness(node),
        source_path=source_path,
        source_line=getattr(node, "lineno", 1),
    )


def inventory_public_stub_modules(stubs_dir: Path) -> list[StubModule]:
    if not stubs_dir.exists():
        return []

    modules: list[StubModule] = []
    for path in sorted(stubs_dir.rglob("*.pyi")):
        module_name = pyi_module_name(stubs_dir, path)
        if not module_name:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise DocumentationSchemaError(f"{path}: invalid public stub syntax: {exc}") from exc

        symbols: list[StubSymbol] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    public_stub_symbol(
                        module_name=module_name,
                        source_path=str(path),
                        node=node,
                    )
                )
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    public_stub_symbol(
                        module_name=module_name,
                        source_path=str(path),
                        node=node,
                    )
                )
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            public_stub_symbol(
                                module_name=module_name,
                                source_path=str(path),
                                node=item,
                                parent_class=node.name,
                            )
                        )

        modules.append(
            StubModule(
                name=module_name,
                source_path=str(path),
                source_line=1,
                symbols=tuple(symbols),
            )
        )
    return modules


def normalized_model(
    methods: list[BindingMethod],
    public_stub_modules: list[StubModule] | None = None,
    metadata_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    module_methods, type_methods, unknown_methods = group_methods(methods)
    modules: dict[str, list[BindingMethod]] = defaultdict(list)

    for module_name, group in module_methods.items():
        modules[module_name].extend(group)
    for type_name, group in type_methods.items():
        modules[f"<type-context>.{type_name}"].extend(group)
    for context_name, group in unknown_methods.items():
        modules[f"<unknown>.{context_name}"].extend(group)

    stub_modules = {module.name: module for module in public_stub_modules or []}
    module_names = sorted(set(modules).union(stub_modules))

    normalized_modules: list[dict[str, Any]] = []
    for module_name in module_names:
        members = [
            {
                "name": method.python_name,
                "qualified_name": f"{module_name}.{method.python_name}",
                "kind": "function",
                "signature": method_signature(method),
                "summary": method_summary(method),
                "doc": method.doc,
                "completeness": completeness_for_doc(method.doc),
                "source_surface": "binding-discovery",
                "source": {
                    "path": method.source,
                    "line": method.line,
                },
            }
            for method in sorted(
                modules[module_name], key=lambda item: (item.python_name, item.line)
            )
        ]
        for symbol in stub_modules.get(module_name, StubModule(module_name, "", 1, ())).symbols:
            members.append(
                {
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "signature": symbol.signature,
                    "summary": symbol.summary,
                    "doc": "",
                    "completeness": symbol.completeness,
                    "source_surface": "public-stub",
                    "source": {
                        "path": symbol.source_path,
                        "line": symbol.source_line,
                    },
                }
            )
        members.sort(key=lambda item: (item["qualified_name"], item["source"]["line"]))
        stub_module = stub_modules.get(module_name)
        counts = Counter(member["completeness"] for member in members)
        source_surface = "public-stub" if stub_module else "binding-discovery"
        if stub_module and not members:
            module_completeness = "discovered"
        elif counts.get("documented", 0) == 0 and counts.get("typed", 0) == 0:
            module_completeness = "missing"
        else:
            module_completeness = "partial"
        normalized_modules.append(
            {
                "name": module_name,
                "summary": "",
                "completeness": module_completeness,
                "source_surface": source_surface,
                "source": {
                    "path": stub_module.source_path if stub_module else "",
                    "line": stub_module.source_line if stub_module else 1,
                },
                "members": members,
            }
        )

    model = {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "metadata": {
            "generator": "src/Tools/typing/generate_stubs.py generate-docs",
            "source": "FreeCAD Python binding discovery",
        },
        "modules": normalized_modules,
    }
    return apply_curated_metadata(model, metadata_documents or [])


def symbol_lookup(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        member["qualified_name"]: member
        for module in model.get("modules", [])
        for member in module.get("members", [])
    }


def document_api_payload(metadata: dict[str, Any], location: str) -> dict[str, list[dict[str, Any]]]:
    value = metadata.get("document_api", {})
    if value == {}:
        return {key: [] for key in DOCUMENT_API_CATEGORIES}
    if not isinstance(value, dict):
        raise DocumentationSchemaError(f"{location}.document_api must be an object")
    payload: dict[str, list[dict[str, Any]]] = {}
    for key in DOCUMENT_API_CATEGORIES:
        payload[key] = metadata_records(value, key, f"{location}.document_api")
    return payload


def curated_entry(
    record: dict[str, Any],
    *,
    category: str,
    source_path: str,
    known_symbols: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    entry = dict(record)
    entry.setdefault("source_surface", "curated-metadata")
    entry.setdefault("source", {"path": source_path, "line": 1})
    entry.setdefault("completeness", "documented")

    if category == "symbols":
        qualified_name = entry.get("qualified_name")
        symbol = known_symbols.get(qualified_name) if isinstance(qualified_name, str) else None
        if symbol:
            entry.setdefault("name", symbol["name"])
            entry.setdefault("kind", symbol["kind"])
            entry.setdefault("signature", symbol["signature"])
            entry.setdefault("summary", symbol.get("summary", ""))
        else:
            entry.setdefault("name", qualified_name or "")
            entry.setdefault("kind", "symbol")
            entry.setdefault("signature", "")
            entry.setdefault("summary", "")
    return entry


def apply_curated_metadata(
    model: dict[str, Any],
    metadata_documents: list[dict[str, Any]],
) -> dict[str, Any]:
    if not metadata_documents:
        return model

    known_symbols = symbol_lookup(model)
    document_api = {key: [] for key in DOCUMENT_API_CATEGORIES}
    for index, metadata in enumerate(metadata_documents):
        source_path = str(metadata.get("source", f"metadata[{index}]"))
        payload = document_api_payload(metadata, f"metadata[{index}]")
        for category, records in payload.items():
            document_api[category].extend(
                curated_entry(
                    record,
                    category=category,
                    source_path=source_path,
                    known_symbols=known_symbols,
                )
                for record in records
            )

    if any(document_api.values()):
        model = dict(model)
        model["document_api"] = document_api
    return model


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
                    "source_surface": member.get("source_surface", "binding-discovery"),
                    "source": member["source"],
                }
            )

    index: dict[str, Any] = {
        "schema_version": AGENT_INDEX_SCHEMA_VERSION,
        "source_schema_version": model["schema_version"],
        "symbols": sorted(symbols, key=lambda symbol: symbol["qualified_name"]),
    }
    if model.get("document_api"):
        index["document_api"] = model["document_api"]
    return index


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
            lines.append(
                f"Source surface: ``{member.get('source_surface', 'binding-discovery')}``"
            )
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

    if model.get("document_api"):
        document_api_path = sphinx_dir / DOCUMENT_API_RST_NAME
        document_api_path.write_text(render_document_api_rst(model), encoding="utf-8")
        written.append(document_api_path)
        module_pages.append(("Document API Tracer Path", DOCUMENT_API_RST_NAME))

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


def render_field_list(record: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for key in keys:
        value = record.get(key)
        if value in (None, "", []):
            continue
        title = key.replace("_", " ").title()
        if isinstance(value, list):
            rendered = ", ".join(f"``{item}``" for item in value)
        else:
            rendered = str(value)
        lines.append(f":{title}: {rendered}")
    if lines:
        lines.append("")
    return lines


def render_document_api_rst(model: dict[str, Any]) -> str:
    document_api = model.get("document_api", {})
    lines = rst_heading("Document API Tracer Path", "=")
    lines.extend([".. This file is generated from curated Document API metadata.", ""])

    sections = (
        ("Modules", "modules", ("name", "summary", "completeness")),
        (
            "Symbols",
            "symbols",
            ("qualified_name", "kind", "signature", "summary", "examples", "completeness"),
        ),
        ("Objects", "objects", ("name", "behavior", "symbols", "properties", "completeness")),
        (
            "Properties",
            "properties",
            ("name", "object_type", "behavior", "symbols", "completeness"),
        ),
        ("Checked Recipes", "examples", ("title", "path", "symbols", "checks", "completeness")),
        ("Anti-Patterns", "anti_patterns", ("name", "summary", "guidance", "symbols")),
    )
    for title, key, fields in sections:
        records = document_api.get(key, [])
        if not records:
            continue
        lines.extend(rst_heading(title, "-"))
        for record in records:
            label = (
                record.get("qualified_name")
                or record.get("title")
                or record.get("name")
                or record.get("path")
                or key
            )
            lines.extend(rst_heading(str(label), "~"))
            lines.extend(render_field_list(record, fields))
            path = record.get("path")
            if isinstance(path, str) and path:
                lines.append(f"Recipe file: ``{path}``")
                lines.append("")
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DocumentationSchemaError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DocumentationSchemaError(f"{path}: top-level JSON value must be an object")
    return payload


def load_metadata_document(path: Path) -> dict[str, Any]:
    """Load CI validation metadata.

    The curated metadata files used by this early pipeline slice are restricted
    to JSON-compatible YAML. JSON is accepted directly, which keeps validation
    dependency-free while still allowing the files to move to fuller YAML later.
    """

    try:
        return load_json(path)
    except DocumentationSchemaError as exc:
        raise DocumentationSchemaError(
            f"{path}: invalid JSON-compatible YAML metadata: {exc}"
        ) from exc


def require_string(container: dict[str, Any], key: str, location: str) -> None:
    if not isinstance(container.get(key), str):
        raise DocumentationSchemaError(f"{location}.{key} must be a string")


def require_object(container: dict[str, Any], key: str, location: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise DocumentationSchemaError(f"{location}.{key} must be an object")
    return value


def require_bool(container: dict[str, Any], key: str, location: str) -> bool:
    value = container.get(key)
    if not isinstance(value, bool):
        raise DocumentationSchemaError(f"{location}.{key} must be a boolean")
    return value


def require_string_list(container: dict[str, Any], key: str, location: str) -> list[str]:
    value = container.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DocumentationSchemaError(f"{location}.{key} must be a list of strings")
    return value


def require_completeness(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise DocumentationSchemaError(f"{location} must be a string")
    if value not in VALID_COMPLETENESS_STATES:
        allowed = ", ".join(sorted(VALID_COMPLETENESS_STATES))
        raise DocumentationSchemaError(
            f"{location} has invalid completeness value {value!r}; "
            f"expected one of {allowed}"
        )
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
        require_completeness(module.get("completeness"), f"{location}.completeness")
        members = module.get("members")
        if not isinstance(members, list):
            raise DocumentationSchemaError(f"{location}.members must be a list")
        for member_index, member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            if not isinstance(member, dict):
                raise DocumentationSchemaError(f"{member_location} must be an object")
            for key in ("name", "qualified_name", "kind", "signature", "completeness"):
                require_string(member, key, member_location)
            require_completeness(
                member.get("completeness"), f"{member_location}.completeness"
            )
            require_object(member, "source", member_location)
    if "document_api" in model:
        validate_document_api_section(model["document_api"], "document_api")


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
        require_completeness(symbol.get("completeness"), f"{location}.completeness")
        require_object(symbol, "source", location)
    if "document_api" in index:
        validate_document_api_section(index["document_api"], "document_api")


def validate_document_api_section(value: Any, location: str) -> None:
    if not isinstance(value, dict):
        raise DocumentationSchemaError(f"{location} must be an object")
    for key in DOCUMENT_API_CATEGORIES:
        records = value.get(key, [])
        if not isinstance(records, list):
            raise DocumentationSchemaError(f"{location}.{key} must be a list")
        for index, record in enumerate(records):
            record_location = f"{location}.{key}[{index}]"
            if not isinstance(record, dict):
                raise DocumentationSchemaError(f"{record_location} must be an object")
            if "completeness" in record:
                require_completeness(
                    record.get("completeness"), f"{record_location}.completeness"
                )
            if key == "modules":
                require_string(record, "name", record_location)
            elif key == "symbols":
                require_string(record, "qualified_name", record_location)
                require_string(record, "kind", record_location)
                require_string(record, "signature", record_location)
            elif key == "objects":
                require_string(record, "name", record_location)
            elif key == "properties":
                require_string(record, "name", record_location)
                require_string(record, "object_type", record_location)
            elif key == "examples":
                require_string(record, "path", record_location)
            elif key == "anti_patterns":
                require_string(record, "name", record_location)


def model_facts(payload: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    validate_documentation_json(payload)
    modules: set[str] = set()
    symbols: dict[str, str] = {}
    if payload.get("schema_version") == NORMALIZED_SCHEMA_VERSION:
        for module in payload.get("modules", []):
            modules.add(module["name"])
            for member in module.get("members", []):
                symbols[member["qualified_name"]] = member["kind"]
    else:
        for symbol in payload.get("symbols", []):
            qualified_name = symbol["qualified_name"]
            modules.add(qualified_name.split(".", 1)[0])
            symbols[qualified_name] = symbol["kind"]
    return modules, symbols


def metadata_records(
    metadata: dict[str, Any],
    key: str,
    location: str,
) -> list[dict[str, Any]]:
    value = metadata.get(key, [])
    if not isinstance(value, list):
        raise DocumentationSchemaError(f"{location}.{key} must be a list")
    records: list[dict[str, Any]] = []
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise DocumentationSchemaError(f"{location}.{key}[{index}] must be an object")
        records.append(record)
    return records


def validate_symbol_reference(
    qualified_name: str,
    symbols: dict[str, str],
    location: str,
) -> None:
    if qualified_name not in symbols:
        raise DocumentationSchemaError(
            f"{location} references unknown symbol {qualified_name!r}"
        )


def validate_curated_metadata(
    metadata: dict[str, Any],
    *,
    modules: set[str],
    symbols: dict[str, str],
    location: str,
) -> tuple[set[str], set[str], list[str]]:
    metadata_symbol_names: set[str] = set()
    example_names: set[str] = set()
    object_type_names: set[str] = set()
    workbench_names: set[str] = set()
    gaps: list[str] = []

    document_api = document_api_payload(metadata, location)

    for index, record in enumerate(metadata_records(metadata, "examples", location)):
        record_location = f"{location}.examples[{index}]"
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise DocumentationSchemaError(f"{record_location}.path must be a non-empty string")
        example_names.add(path)
        require_string_list(record, "symbols", record_location)
        for symbol_name in record.get("symbols", []):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        if "required" in record:
            require_bool(record, "required", record_location)
        require_string_list(record, "checks", record_location)

    for index, record in enumerate(metadata_records(metadata, "object_types", location)):
        record_location = f"{location}.object_types[{index}]"
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        object_type_names.add(name)
        if name in symbols and symbols[name] != "class":
            raise DocumentationSchemaError(f"{record_location}.name must reference a class symbol")
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")

    for index, record in enumerate(document_api["modules"]):
        record_location = f"{location}.document_api.modules[{index}]"
        module_name = record.get("name")
        if not isinstance(module_name, str) or not module_name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        if module_name not in modules:
            raise DocumentationSchemaError(
                f"{record_location}.name references unknown module {module_name!r}"
            )
        if "completeness" in record:
            state = require_completeness(record.get("completeness"), f"{record_location}.completeness")
            if state in {"missing", "discovered"}:
                gaps.append(f"document_api module {module_name}: {state}")

    for index, record in enumerate(document_api["objects"]):
        record_location = f"{location}.document_api.objects[{index}]"
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        object_type_names.add(name)
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        require_string_list(record, "properties", record_location)
        if "completeness" in record:
            state = require_completeness(record.get("completeness"), f"{record_location}.completeness")
            if state in {"missing", "discovered"}:
                gaps.append(f"document_api object {name}: {state}")

    for index, record in enumerate(document_api["properties"]):
        record_location = f"{location}.document_api.properties[{index}]"
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        object_type = record.get("object_type")
        if not isinstance(object_type, str) or not object_type:
            raise DocumentationSchemaError(
                f"{record_location}.object_type must be a non-empty string"
            )
        if object_type not in object_type_names:
            raise DocumentationSchemaError(
                f"{record_location}.object_type references unknown object type {object_type!r}"
            )
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        if "completeness" in record:
            state = require_completeness(record.get("completeness"), f"{record_location}.completeness")
            if state in {"missing", "discovered"}:
                gaps.append(f"document_api property {name}: {state}")

    document_example_names: set[str] = set()
    for index, record in enumerate(document_api["examples"]):
        record_location = f"{location}.document_api.examples[{index}]"
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise DocumentationSchemaError(f"{record_location}.path must be a non-empty string")
        document_example_names.add(path)
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        if "required" in record:
            require_bool(record, "required", record_location)
        require_string_list(record, "checks", record_location)
        if "completeness" in record:
            state = require_completeness(record.get("completeness"), f"{record_location}.completeness")
            if state in {"missing", "discovered"}:
                gaps.append(f"document_api example {path}: {state}")

    anti_pattern_names: set[str] = set()
    for index, record in enumerate(document_api["anti_patterns"]):
        record_location = f"{location}.document_api.anti_patterns[{index}]"
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        anti_pattern_names.add(name)
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        if "completeness" in record:
            state = require_completeness(record.get("completeness"), f"{record_location}.completeness")
            if state in {"missing", "discovered"}:
                gaps.append(f"document_api anti-pattern {name}: {state}")

    for index, record in enumerate(document_api["symbols"]):
        record_location = f"{location}.document_api.symbols[{index}]"
        qualified_name = record.get("qualified_name")
        if not isinstance(qualified_name, str) or not qualified_name:
            raise DocumentationSchemaError(
                f"{record_location}.qualified_name must be a non-empty string"
            )
        metadata_symbol_names.add(qualified_name)
        validate_symbol_reference(qualified_name, symbols, f"{record_location}.qualified_name")
        if "completeness" in record:
            completeness = require_completeness(
                record.get("completeness"), f"{record_location}.completeness"
            )
            if completeness in {"missing", "discovered"}:
                gaps.append(f"{qualified_name}: {completeness}")
        for example_name in require_string_list(record, "examples", record_location):
            if example_name not in document_example_names and example_name not in example_names:
                raise DocumentationSchemaError(
                    f"{record_location}.examples references unknown example {example_name!r}"
                )
        for object_type in require_string_list(record, "objects", record_location):
            if object_type not in object_type_names:
                raise DocumentationSchemaError(
                    f"{record_location}.objects references unknown object type {object_type!r}"
                )
        for anti_pattern in require_string_list(record, "anti_patterns", record_location):
            if anti_pattern not in anti_pattern_names:
                raise DocumentationSchemaError(
                    f"{record_location}.anti_patterns references unknown anti-pattern "
                    f"{anti_pattern!r}"
                )

    for index, record in enumerate(metadata_records(metadata, "workbenches", location)):
        record_location = f"{location}.workbenches[{index}]"
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DocumentationSchemaError(f"{record_location}.name must be a non-empty string")
        workbench_names.add(name)
        for module_name in require_string_list(record, "modules", record_location):
            if module_name not in modules:
                raise DocumentationSchemaError(
                    f"{record_location}.modules references unknown module {module_name!r}"
                )
        for symbol_name in require_string_list(record, "symbols", record_location):
            validate_symbol_reference(symbol_name, symbols, f"{record_location}.symbols")
        for object_type in require_string_list(record, "object_types", record_location):
            if object_type not in object_type_names:
                raise DocumentationSchemaError(
                    f"{record_location}.object_types references unknown object type {object_type!r}"
                )

    for index, record in enumerate(metadata_records(metadata, "symbols", location)):
        record_location = f"{location}.symbols[{index}]"
        qualified_name = record.get("qualified_name")
        if not isinstance(qualified_name, str) or not qualified_name:
            raise DocumentationSchemaError(
                f"{record_location}.qualified_name must be a non-empty string"
            )
        metadata_symbol_names.add(qualified_name)
        validate_symbol_reference(qualified_name, symbols, f"{record_location}.qualified_name")
        if "completeness" in record:
            completeness = require_completeness(
                record.get("completeness"), f"{record_location}.completeness"
            )
            if completeness in {"missing", "discovered"}:
                gaps.append(f"{qualified_name}: {completeness}")
        for example_name in require_string_list(record, "examples", record_location):
            if example_name not in example_names:
                raise DocumentationSchemaError(
                    f"{record_location}.examples references unknown example {example_name!r}"
                )
        for object_type in require_string_list(record, "object_types", record_location):
            if object_type not in object_type_names:
                raise DocumentationSchemaError(
                    f"{record_location}.object_types references unknown object type {object_type!r}"
                )
        for workbench_name in require_string_list(record, "workbenches", record_location):
            if workbench_name not in workbench_names:
                raise DocumentationSchemaError(
                    f"{record_location}.workbenches references unknown workbench {workbench_name!r}"
                )

    for index, record in enumerate(metadata_records(metadata, "completeness", location)):
        record_location = f"{location}.completeness[{index}]"
        qualified_name = record.get("qualified_name")
        if not isinstance(qualified_name, str) or not qualified_name:
            raise DocumentationSchemaError(
                f"{record_location}.qualified_name must be a non-empty string"
            )
        validate_symbol_reference(qualified_name, symbols, f"{record_location}.qualified_name")
        state = require_completeness(record.get("state"), f"{record_location}.state")
        if state in {"missing", "discovered"}:
            gaps.append(f"{qualified_name}: {state}")
        if "reason" in record:
            require_string(record, "reason", record_location)

    metadata_records(metadata, "runtime_exceptions", location)
    return metadata_symbol_names, example_names, gaps


def validate_checked_examples(
    metadata_documents: list[dict[str, Any]],
    examples_path: Path | None,
    freecad_executable: Path | None = None,
) -> None:
    for metadata_index, metadata in enumerate(metadata_documents):
        location = f"metadata[{metadata_index}]"
        top_level_examples = metadata_records(metadata, "examples", location)
        examples = list(top_level_examples)
        examples.extend(document_api_payload(metadata, location)["examples"])
        for index, record in enumerate(examples):
            nested = index >= len(top_level_examples)
            record_location = (
                f"{location}.document_api.examples[{index - len(top_level_examples)}]"
                if nested
                else f"{location}.examples[{index}]"
            )
            validate_checked_example(
                record,
                record_location,
                examples_path,
                freecad_executable,
            )


def resolve_freecad_executable(path: Path | None) -> str | None:
    if path is not None:
        return str(path)
    env_value = os.environ.get("FREECAD_CMD")
    if env_value:
        return env_value
    return shutil.which("FreeCADCmd") or shutil.which("freecadcmd") or shutil.which("FreeCAD")


def validate_checked_example(
    record: dict[str, Any],
    record_location: str,
    examples_path: Path | None,
    freecad_executable: Path | None,
) -> None:
    required = bool(record.get("required", False))
    checks = set(require_string_list(record, "checks", record_location))
    if not required:
        return
    if examples_path is None:
        raise DocumentationSchemaError(
            f"{record_location} required example needs --checked-examples"
        )
    unsupported_checks = checks.difference({"syntax", "freecad-headless"})
    if unsupported_checks:
        raise DocumentationSchemaError(
            f"{record_location}.checks contains unsupported checks: "
            f"{sorted(unsupported_checks)}"
        )
    example = Path(record["path"])
    path = example if example.is_absolute() else examples_path / example
    if not path.exists():
        raise DocumentationSchemaError(f"{record_location} required example is missing: {path}")
    if "syntax" in checks:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise DocumentationSchemaError(
                f"{record_location} required example failed syntax check: {exc}"
            ) from exc
    if "freecad-headless" in checks:
        executable = resolve_freecad_executable(freecad_executable)
        if executable is None:
            return
        command = (
            [sys.executable, executable, str(path)]
            if Path(executable).suffix == ".py"
            else [executable, str(path)]
        )
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DocumentationSchemaError(
                f"{record_location} required example failed headless FreeCAD smoke "
                f"execution with {executable}: {result.stderr.strip()}"
            )


def exception_records(
    metadata_documents: list[dict[str, Any]],
    accepted_exceptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for metadata_index, metadata in enumerate(metadata_documents):
        records.extend(
            metadata_records(
                metadata, "runtime_exceptions", f"metadata[{metadata_index}]"
            )
        )
    for exception_index, metadata in enumerate(accepted_exceptions):
        if "runtime_exceptions" in metadata:
            records.extend(
                metadata_records(
                    metadata,
                    "runtime_exceptions",
                    f"accepted_exceptions[{exception_index}]",
                )
            )
            continue
        if not isinstance(metadata.get("exceptions", []), list):
            raise DocumentationSchemaError(
                f"accepted_exceptions[{exception_index}].exceptions must be a list"
            )
        records.extend(
            metadata_records(
                metadata, "exceptions", f"accepted_exceptions[{exception_index}]"
            )
        )
    return records


def runtime_exception_matches(
    exception: dict[str, Any],
    qualified_name: str,
    runtime_kind: str,
    documented_kind: str,
) -> bool:
    if exception.get("qualified_name") != qualified_name:
        return False
    expected_runtime = exception.get("runtime_kind")
    expected_documented = exception.get("documented_kind")
    return (
        (expected_runtime is None or expected_runtime == runtime_kind)
        and (expected_documented is None or expected_documented == documented_kind)
    )


def runtime_availability_exception_matches(
    exception: dict[str, Any],
    *,
    object_type: str,
    property_name: str | None = None,
    available: bool | None = None,
) -> bool:
    if exception.get("object_type") != object_type:
        return False
    expected_property = exception.get("property_name", exception.get("property"))
    if property_name is None and expected_property not in (None, ""):
        return False
    if property_name is not None and expected_property not in (None, property_name):
        return False
    expected_available = exception.get("available")
    return expected_available is None or expected_available == available


def documented_document_api_objects(
    metadata_documents: list[dict[str, Any]],
) -> tuple[set[str], dict[tuple[str, str], dict[str, Any]]]:
    object_types: set[str] = set()
    properties: dict[tuple[str, str], dict[str, Any]] = {}
    for metadata_index, metadata in enumerate(metadata_documents):
        document_api = document_api_payload(metadata, f"metadata[{metadata_index}]")
        for record in document_api["objects"]:
            name = record.get("name")
            if isinstance(name, str) and name:
                object_types.add(name)
        for record in document_api["properties"]:
            object_type = record.get("object_type")
            name = record.get("name")
            if isinstance(object_type, str) and object_type and isinstance(name, str) and name:
                object_types.add(object_type)
                properties[(object_type, name)] = record
    return object_types, properties


def inventory_document_api_objects(runtime_inventory: dict[str, Any]) -> list[dict[str, Any]]:
    document_api = runtime_inventory.get("document_api", {})
    if document_api == {}:
        return []
    if not isinstance(document_api, dict):
        raise DocumentationSchemaError("runtime inventory document_api must be an object")
    objects = document_api.get("objects", [])
    if not isinstance(objects, list):
        raise DocumentationSchemaError("runtime inventory document_api.objects must be a list")
    for index, record in enumerate(objects):
        if not isinstance(record, dict):
            raise DocumentationSchemaError(
                f"runtime_inventory.document_api.objects[{index}] must be an object"
            )
    return objects


def validate_runtime_inventory(
    runtime_inventory: dict[str, Any] | None,
    *,
    symbols: dict[str, str],
    metadata_documents: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
) -> tuple[str, ...]:
    if runtime_inventory is None:
        return ()
    notes: list[str] = []
    runtime_symbols = runtime_inventory.get("symbols", [])
    if not isinstance(runtime_symbols, list):
        raise DocumentationSchemaError("runtime inventory symbols must be a list")
    for index, record in enumerate(runtime_symbols):
        location = f"runtime_inventory.symbols[{index}]"
        if not isinstance(record, dict):
            raise DocumentationSchemaError(f"{location} must be an object")
        qualified_name = record.get("qualified_name")
        runtime_kind = record.get("kind")
        if not isinstance(qualified_name, str) or not qualified_name:
            raise DocumentationSchemaError(f"{location}.qualified_name must be a non-empty string")
        if not isinstance(runtime_kind, str) or not runtime_kind:
            raise DocumentationSchemaError(f"{location}.kind must be a non-empty string")
        documented_kind = symbols.get(qualified_name)
        if documented_kind is None or documented_kind == runtime_kind:
            continue
        if any(
            runtime_exception_matches(
                exception, qualified_name, runtime_kind, documented_kind
            )
            for exception in exceptions
        ):
            continue
        raise DocumentationSchemaError(
            f"{location} runtime kind {runtime_kind!r} conflicts with documented kind "
            f"{documented_kind!r} for {qualified_name!r}"
        )
    documented_objects, documented_properties = documented_document_api_objects(
        metadata_documents
    )
    for object_index, record in enumerate(inventory_document_api_objects(runtime_inventory)):
        location = f"runtime_inventory.document_api.objects[{object_index}]"
        object_type = record.get("name")
        if not isinstance(object_type, str) or not object_type:
            raise DocumentationSchemaError(f"{location}.name must be a non-empty string")
        available = record.get("available", True)
        if not isinstance(available, bool):
            raise DocumentationSchemaError(f"{location}.available must be a boolean")
        if object_type in documented_objects and not available:
            note = f"Document API runtime availability: object {object_type} is unavailable"
            notes.append(note)
            if not any(
                runtime_availability_exception_matches(
                    exception,
                    object_type=object_type,
                    available=available,
                )
                for exception in exceptions
            ):
                raise DocumentationSchemaError(f"{location} {note}")
        properties = record.get("properties", [])
        if not isinstance(properties, list):
            raise DocumentationSchemaError(f"{location}.properties must be a list")
        for property_index, property_record in enumerate(properties):
            property_location = f"{location}.properties[{property_index}]"
            if not isinstance(property_record, dict):
                raise DocumentationSchemaError(f"{property_location} must be an object")
            property_name = property_record.get("name")
            property_available = property_record.get("available", True)
            if not isinstance(property_name, str) or not property_name:
                raise DocumentationSchemaError(
                    f"{property_location}.name must be a non-empty string"
                )
            if not isinstance(property_available, bool):
                raise DocumentationSchemaError(f"{property_location}.available must be a boolean")
            if (object_type, property_name) not in documented_properties:
                continue
            if property_available:
                continue
            note = (
                "Document API runtime availability: property "
                f"{object_type}.{property_name} is unavailable"
            )
            notes.append(note)
            if any(
                runtime_availability_exception_matches(
                    exception,
                    object_type=object_type,
                    property_name=property_name,
                    available=property_available,
                )
                for exception in exceptions
            ):
                continue
            raise DocumentationSchemaError(f"{property_location} {note}")
    return tuple(notes)


def collect_document_api_runtime_inventory(
    metadata_documents: list[dict[str, Any]],
    *,
    freecad_executable: Path | None = None,
) -> dict[str, Any]:
    executable = resolve_freecad_executable(freecad_executable)
    if executable is None:
        raise DocumentationSchemaError(
            "runtime inventory collection needs --freecad-executable or FREECAD_CMD"
        )
    object_types, properties = documented_document_api_objects(metadata_documents)
    expectations = [
        {
            "name": object_type,
            "properties": sorted(
                property_name
                for candidate, property_name in properties
                if candidate == object_type
            ),
        }
        for object_type in sorted(object_types)
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        script_path = tmp_dir / "collect_document_api_runtime_inventory.py"
        expectations_path = tmp_dir / "expectations.json"
        output_path = tmp_dir / "runtime-inventory.json"
        expectations_path.write_text(json.dumps(expectations), encoding="utf-8")
        script_path.write_text(RUNTIME_INVENTORY_SCRIPT, encoding="utf-8")
        command = (
            [sys.executable, executable, str(script_path), str(expectations_path), str(output_path)]
            if Path(executable).suffix == ".py"
            else [executable, str(script_path), str(expectations_path), str(output_path)]
        )
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DocumentationSchemaError(
                "runtime inventory collection failed with "
                f"{executable}: {result.stderr.strip()}"
            )
        return load_json(output_path)


RUNTIME_INVENTORY_SCRIPT = r'''
import json
import re
import sys

import FreeCAD


def property_available(obj, name):
    if hasattr(obj, name):
        return True
    properties = getattr(obj, "PropertiesList", [])
    return isinstance(properties, (list, tuple)) and name in properties


def safe_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return cleaned or "Object"


def main():
    expectations_path, output_path = sys.argv[1:3]
    expectations = json.loads(open(expectations_path, encoding="utf-8").read())
    objects = []
    doc = None
    try:
        doc = FreeCAD.newDocument("DocRuntimeInventory")
        for expectation in expectations:
            object_type = expectation["name"]
            properties = expectation.get("properties", [])
            record = {"name": object_type, "available": True, "properties": []}
            try:
                obj = doc if object_type == "App::Document" else doc.addObject(
                    object_type, safe_name(object_type)
                )
            except Exception as exc:
                record["available"] = False
                record["error"] = str(exc)
                obj = None
            if obj is not None:
                for property_name in properties:
                    record["properties"].append(
                        {
                            "name": property_name,
                            "available": property_available(obj, property_name),
                        }
                    )
            objects.append(record)
    finally:
        if doc is not None and hasattr(FreeCAD, "closeDocument"):
            try:
                FreeCAD.closeDocument(getattr(doc, "Name", "DocRuntimeInventory"))
            except Exception:
                pass
    payload = {
        "schema_version": "freecad-doc-runtime-inventory-v1",
        "document_api": {"objects": objects},
    }
    open(output_path, "w", encoding="utf-8").write(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
'''


def validate_documentation_inputs(
    payload: dict[str, Any],
    *,
    metadata_documents: list[dict[str, Any]] | None = None,
    checked_examples_path: Path | None = None,
    runtime_inventory: dict[str, Any] | None = None,
    accepted_exceptions: list[dict[str, Any]] | None = None,
    freecad_executable: Path | None = None,
) -> DocumentationValidationResult:
    modules, symbols = model_facts(payload)
    metadata_documents = metadata_documents or []
    accepted_exceptions = accepted_exceptions or []
    gaps: list[str] = []
    for index, metadata in enumerate(metadata_documents):
        _, _, metadata_gaps = validate_curated_metadata(
            metadata,
            modules=modules,
            symbols=symbols,
            location=f"metadata[{index}]",
        )
        gaps.extend(metadata_gaps)
    validate_checked_examples(metadata_documents, checked_examples_path, freecad_executable)
    availability_notes = validate_runtime_inventory(
        runtime_inventory,
        symbols=symbols,
        metadata_documents=metadata_documents,
        exceptions=exception_records(metadata_documents, accepted_exceptions),
    )
    return DocumentationValidationResult(
        completeness_gaps=tuple(gaps),
        availability_notes=availability_notes,
    )


def surface_completeness_counts(model: dict[str, Any], source_surface: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for module in model.get("modules", []):
        if module.get("source_surface") == source_surface:
            counts[f"module:{module.get('completeness', 'unknown')}"] += 1
        for member in module.get("members", []):
            if member.get("source_surface") == source_surface:
                counts[f"symbol:{member.get('completeness', 'unknown')}"] += 1
    return counts


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


def document_api_completeness_counts(model: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for category, records in model.get("document_api", {}).items():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                counts[f"{category}:{record.get('completeness', 'unknown')}"] += 1
    return counts


def document_api_missing_docs(model: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for category, records in model.get("document_api", {}).items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            state = record.get("completeness")
            if state not in {"missing", "discovered"}:
                continue
            name = record.get("qualified_name") or record.get("name") or record.get("path")
            missing.append(f"{category}: {name}")
    return sorted(missing)


def document_api_missing_examples(model: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    example_paths = {
        record.get("path")
        for record in model.get("document_api", {}).get("examples", [])
        if isinstance(record, dict)
    }
    for record in model.get("document_api", {}).get("symbols", []):
        if not isinstance(record, dict):
            continue
        examples = record.get("examples", [])
        if not isinstance(examples, list) or not examples:
            missing.append(str(record.get("qualified_name", "")))
            continue
        if any(example not in example_paths for example in examples):
            missing.append(str(record.get("qualified_name", "")))
    return sorted(name for name in missing if name)


def quality_report(model: dict[str, Any]) -> str:
    validate_normalized_model(model)
    counts = completeness_counts(model)
    public_stub_counts = surface_completeness_counts(model, "public-stub")
    document_counts = document_api_completeness_counts(model)
    document_missing_docs = document_api_missing_docs(model)
    document_missing_examples = document_api_missing_examples(model)
    document_entry_total = sum(document_counts.values())
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
    lines.extend(["", "## Public Stub Surface Counts", ""])
    if public_stub_counts:
        for state, count in sorted(public_stub_counts.items()):
            lines.append(f"- `{state}`: {count}")
    else:
        lines.append("- `none`: 0")
    lines.extend(["", "## Document API Status", ""])
    lines.append(f"- Status: `{'present' if document_entry_total else 'absent'}`")
    lines.append(f"- Entries: {document_entry_total}")
    lines.append(f"- Missing docs: {len(document_missing_docs)}")
    lines.append(f"- Missing examples: {len(document_missing_examples)}")
    lines.extend(["", "## Document API Completeness Counts", ""])
    if document_counts:
        for state, count in sorted(document_counts.items()):
            lines.append(f"- `{state}`: {count}")
    else:
        lines.append("- `none`: 0")
    lines.extend(["", "## Document API Missing Docs", ""])
    if document_missing_docs:
        for item in document_missing_docs:
            lines.append(f"- {item}")
    else:
        lines.append("- `none`")
    lines.extend(["", "## Document API Missing Examples", ""])
    if document_missing_examples:
        for item in document_missing_examples:
            lines.append(f"- {item}")
    else:
        lines.append("- `none`")
    lines.append("")
    return "\n".join(lines)
