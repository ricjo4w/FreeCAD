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
VALID_COMPLETENESS_STATES = frozenset(
    {"documented", "missing", "typed", "discovered", "partial"}
)


class DocumentationSchemaError(ValueError):
    """Raised when a generated documentation JSON file violates its schema."""


@dataclass(frozen=True)
class DocumentationValidationResult:
    completeness_gaps: tuple[str, ...] = ()


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
                    "source_surface": member.get("source_surface", "binding-discovery"),
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
) -> None:
    for metadata_index, metadata in enumerate(metadata_documents):
        location = f"metadata[{metadata_index}]"
        for index, record in enumerate(metadata_records(metadata, "examples", location)):
            record_location = f"{location}.examples[{index}]"
            required = bool(record.get("required", False))
            checks = set(require_string_list(record, "checks", record_location))
            if not required:
                continue
            if examples_path is None:
                raise DocumentationSchemaError(
                    f"{record_location} required example needs --checked-examples"
                )
            unsupported_checks = checks.difference({"syntax"})
            if unsupported_checks:
                raise DocumentationSchemaError(
                    f"{record_location}.checks contains unsupported checks: "
                    f"{sorted(unsupported_checks)}"
                )
            example = Path(record["path"])
            path = example if example.is_absolute() else examples_path / example
            if not path.exists():
                raise DocumentationSchemaError(
                    f"{record_location} required example is missing: {path}"
                )
            if "syntax" in checks:
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except SyntaxError as exc:
                    raise DocumentationSchemaError(
                        f"{record_location} required example failed syntax check: {exc}"
                    ) from exc


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


def validate_runtime_inventory(
    runtime_inventory: dict[str, Any] | None,
    *,
    symbols: dict[str, str],
    exceptions: list[dict[str, Any]],
) -> None:
    if runtime_inventory is None:
        return
    runtime_symbols = runtime_inventory.get("symbols")
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


def validate_documentation_inputs(
    payload: dict[str, Any],
    *,
    metadata_documents: list[dict[str, Any]] | None = None,
    checked_examples_path: Path | None = None,
    runtime_inventory: dict[str, Any] | None = None,
    accepted_exceptions: list[dict[str, Any]] | None = None,
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
    validate_checked_examples(metadata_documents, checked_examples_path)
    validate_runtime_inventory(
        runtime_inventory,
        symbols=symbols,
        exceptions=exception_records(metadata_documents, accepted_exceptions),
    )
    return DocumentationValidationResult(completeness_gaps=tuple(gaps))


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


def quality_report(model: dict[str, Any]) -> str:
    validate_normalized_model(model)
    counts = completeness_counts(model)
    public_stub_counts = surface_completeness_counts(model, "public-stub")
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
    lines.append("")
    return "\n".join(lines)
