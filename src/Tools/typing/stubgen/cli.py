"""Command-line front end for the binding stub generation pipeline.

This module is intentionally thin. Its job is to expose a stable user-facing
entrypoint, resolve repository-relative paths, and dispatch to the generator in
``generate``, ``check``, documentation, or ``lint-docs`` mode.

Keep policy and parsing logic out of this file:
- binding discovery belongs in ``generator``
- syntax helpers belong in ``parsing``
- shared constants and defaults belong in ``model``

The ``check`` flow is also coordinated here so the external tools invoked after
stub generation, and the log files they write, are defined in one place.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from .doc_lint import lint_curated_stub_docs
from .doc_pipeline import (
    AGENT_INDEX_JSON_NAME,
    NORMALIZED_JSON_NAME,
    QUALITY_REPORT_NAME,
    DocumentationSchemaError,
    agent_api_index,
    inventory_public_stub_modules,
    load_json,
    load_metadata_document,
    normalized_model,
    quality_report,
    render_sphinx_rst,
    validate_documentation_inputs,
    validate_documentation_json,
    write_json,
)
from .discovery import collect_methods, collect_type_registrations
from .generator import (
    markdown_report,
    write_outputs,
)
from .model import DEFAULT_OVERLAY_DIR, DEFAULT_SOURCE_DIR, DEFAULT_STUBS_OUT_DIR
from .parsing import iter_source_files
from .source_inputs import collect_binding_classes, load_stub_signature_overrides

DESCRIPTION = """Generate type-checker stubs for FreeCAD Python bindings.

The command inventories hand-written C++ Python registrations, merges them with
binding .pyi class specs and curated overlays, and writes public import-shaped
stubs for type-checker use.
"""

# Keep the smoke checker pinned so CI and local runs resolve the same tool version.
PYREFLY_VERSION = "0.60.2"


def resolve_optional_dir(root: Path, path: Path | None, default: Path | None = None) -> Path | None:
    if path is not None:
        return path if path.is_absolute() else root / path
    if default is None:
        return None
    candidate = root / default
    return candidate if candidate.exists() else None


def add_common_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="FreeCAD source checkout root. Defaults to the current directory.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Source directory to scan, relative to --root unless absolute. Defaults to src.",
    )


def print_stdout(message: str) -> None:
    print(message, end="")


def print_stderr(message: str) -> None:
    print(message, end="", file=sys.stderr)


def add_generation_args(parser: argparse.ArgumentParser) -> None:
    add_common_path_args(parser)
    parser.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "Directory for generated debug skeletons and merged public stub output. "
            "If omitted, print the inventory report as Markdown."
        ),
    )
    parser.add_argument(
        "--overlay-dir",
        type=Path,
        help=(
            "Curated stub overlay directory to apply over generated skeletons. "
            f"Defaults to {DEFAULT_OVERLAY_DIR} when that directory exists."
        ),
    )
    parser.add_argument(
        "--no-overlays",
        action="store_true",
        help="Do not apply curated stub overlays to the merged output.",
    )


def add_docs_source_args(parser: argparse.ArgumentParser) -> None:
    add_common_path_args(parser)
    parser.add_argument(
        "--stubs-dir",
        type=Path,
        default=None,
        help=(
            "Generated public stub directory to inventory for documentation. "
            f"Defaults to {DEFAULT_STUBS_OUT_DIR} when that directory exists."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for generated documentation JSON, RST, and report artifacts.",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv and argv[0] == "check":
        parser = argparse.ArgumentParser(
            description="Generate binding stubs and run the smoke type checks."
        )
        add_generation_args(parser)
        parser.add_argument(
            "--log-dir",
            type=Path,
            help="Optional directory for individual generator and checker logs.",
        )
        parser.set_defaults(command="check")
        return parser.parse_args(argv[1:])

    if argv and argv[0] == "lint-docs":
        parser = argparse.ArgumentParser(
            description="Lint documentation coverage in curated source-adjacent stub inputs."
        )
        add_common_path_args(parser)
        parser.add_argument(
            "--log-dir",
            type=Path,
            help="Optional directory for the documentation lint log.",
        )
        parser.add_argument(
            "paths",
            nargs="*",
            type=Path,
            help=(
                "Optional file or directory paths to lint. Paths are resolved relative to "
                "--root unless absolute."
            ),
        )
        parser.set_defaults(command="lint-docs")
        return parser.parse_args(argv[1:])

    if argv and argv[0] == "generate-docs":
        parser = argparse.ArgumentParser(
            description="Generate normalized Python API documentation artifacts."
        )
        add_docs_source_args(parser)
        parser.set_defaults(command="generate-docs")
        return parser.parse_args(argv[1:])

    if argv and argv[0] == "validate-docs":
        parser = argparse.ArgumentParser(
            description="Validate generated Python API documentation JSON."
        )
        parser.add_argument(
            "json_path",
            type=Path,
            help="Generated documentation JSON to validate.",
        )
        parser.add_argument(
            "--metadata",
            action="append",
            type=Path,
            default=[],
            help="Curated JSON-compatible YAML metadata file to validate against the JSON.",
        )
        parser.add_argument(
            "--checked-examples",
            type=Path,
            default=None,
            help="Directory containing required checked examples referenced by metadata.",
        )
        parser.add_argument(
            "--runtime-inventory",
            type=Path,
            default=None,
            help="Runtime inventory JSON used to detect documented/runtime kind conflicts.",
        )
        parser.add_argument(
            "--accepted-exceptions",
            action="append",
            type=Path,
            default=[],
            help="JSON-compatible YAML file listing accepted runtime metadata exceptions.",
        )
        parser.set_defaults(command="validate-docs")
        return parser.parse_args(argv[1:])

    if argv and argv[0] == "report-docs":
        parser = argparse.ArgumentParser(
            description="Write a documentation quality report from normalized JSON."
        )
        parser.add_argument(
            "normalized_json",
            type=Path,
            help="Normalized documentation JSON generated by generate-docs.",
        )
        parser.add_argument(
            "--out-file",
            type=Path,
            default=None,
            help=(
                "Markdown report path. Defaults to documentation-quality-report.md "
                "next to the input JSON."
            ),
        )
        parser.set_defaults(command="report-docs")
        return parser.parse_args(argv[1:])

    parser = argparse.ArgumentParser(description=DESCRIPTION)
    add_generation_args(parser)
    parser.set_defaults(command="generate")
    return parser.parse_args(argv)


def write_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_logged_command(
    name: str,
    cmd: list[str],
    cwd: Path,
    log_dir: Path | None,
) -> tuple[int, str]:
    if log_dir is None:
        result = subprocess.run(cmd, cwd=cwd)
        return result.returncode, ""

    result = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = result.stdout + result.stderr
    write_log(log_dir / f"{name}.log", output)
    return result.returncode, output


def run_generate(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    source_dir = args.source_dir if args.source_dir.is_absolute() else root / args.source_dir
    if not source_dir.exists():
        print_stderr(f"source directory does not exist: {source_dir}\n")
        return 2

    type_registrations = collect_type_registrations(root, list(iter_source_files(root, source_dir)))
    methods = collect_methods(root, source_dir)
    classes = collect_binding_classes(root, source_dir, type_registrations)
    stub_signature_overrides = load_stub_signature_overrides(
        root,
        source_dir,
        methods,
        type_registrations,
    )
    overlay_dir = (
        None
        if args.no_overlays
        else resolve_optional_dir(root, args.overlay_dir, DEFAULT_OVERLAY_DIR)
    )

    if args.out_dir:
        out_dir = args.out_dir if args.out_dir.is_absolute() else root / args.out_dir
        overlay_count = write_outputs(
            out_dir,
            root,
            source_dir,
            methods,
            classes,
            type_registrations,
            stub_signature_overrides,
            overlay_dir,
        )
        summary = (
            f"Wrote {len(methods)} registrations and {len(classes)} class bindings to {out_dir} "
            f"({overlay_count} overlay stub files applied)"
        )
        print(summary)
        if getattr(args, "log_dir", None):
            log_dir = args.log_dir.resolve()
            write_log(log_dir / "python-stubs-generate.log", summary + "\n")
    else:
        print_stdout(markdown_report(methods))
    return 0


def run_check(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if args.out_dir is None:
        args.out_dir = DEFAULT_STUBS_OUT_DIR
    generation_code = run_generate(args)
    if generation_code != 0:
        return generation_code

    stubs_dir = root / "src/Tools/typing"
    log_dir = args.log_dir.resolve() if args.log_dir else None

    pyright_code, _ = run_logged_command(
        "python-stubs-pyright",
        ["pixi", "run", "pyright", "-p", "smoke/pyrightconfig.json"],
        stubs_dir,
        log_dir,
    )
    pyrefly_code, _ = run_logged_command(
        "python-stubs-pyrefly",
        [
            "uvx",
            "--from",
            f"pyrefly=={PYREFLY_VERSION}",
            "pyrefly",
            "check",
            "--config",
            "smoke/pyrefly.toml",
        ],
        stubs_dir,
        log_dir,
    )
    return 0 if pyright_code == 0 and pyrefly_code == 0 else 1


def run_lint_docs(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    source_dir = args.source_dir if args.source_dir.is_absolute() else root / args.source_dir
    if not source_dir.exists():
        print_stderr(f"source directory does not exist: {source_dir}\n")
        return 2

    selected_paths = tuple(
        path.resolve() if path.is_absolute() else (root / path).resolve() for path in args.paths
    )
    report = lint_curated_stub_docs(root, source_dir, selected_paths)
    output = report.render(root)

    if getattr(args, "log_dir", None):
        log_dir = args.log_dir.resolve()
        write_log(log_dir / "python-stubs-docs.log", output)

    if report.files_checked == 0:
        print_stderr("No curated source-adjacent stub files matched the requested paths.\n")
        return 2

    if report.ok:
        print_stdout(output)
        return 0

    print_stderr(output)
    return 1


def run_generate_docs(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    source_dir = args.source_dir if args.source_dir.is_absolute() else root / args.source_dir
    if not source_dir.exists():
        print_stderr(f"source directory does not exist: {source_dir}\n")
        return 2

    out_dir = args.out_dir if args.out_dir.is_absolute() else root / args.out_dir
    stubs_dir = resolve_optional_dir(root, args.stubs_dir, DEFAULT_STUBS_OUT_DIR)
    methods = collect_methods(root, source_dir)
    public_stub_modules = inventory_public_stub_modules(stubs_dir) if stubs_dir else []
    model = normalized_model(methods, public_stub_modules)
    validate_documentation_json(model)

    index = agent_api_index(model)
    validate_documentation_json(index)

    normalized_path = out_dir / NORMALIZED_JSON_NAME
    index_path = out_dir / AGENT_INDEX_JSON_NAME
    report_path = out_dir / QUALITY_REPORT_NAME
    write_json(normalized_path, model)
    write_json(index_path, index)
    rst_paths = render_sphinx_rst(model, out_dir)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(quality_report(model), encoding="utf-8")

    print(
        "Wrote documentation artifacts to "
        f"{out_dir} ({len(methods)} registrations, "
        f"{len(public_stub_modules)} public stub modules, {len(rst_paths)} RST files)"
    )
    return 0


def run_validate_docs(args: argparse.Namespace) -> int:
    try:
        payload = load_json(args.json_path)
        metadata_documents = [
            load_metadata_document(path) for path in getattr(args, "metadata", [])
        ]
        accepted_exceptions = [
            load_metadata_document(path)
            for path in getattr(args, "accepted_exceptions", [])
        ]
        runtime_inventory = (
            load_json(args.runtime_inventory)
            if getattr(args, "runtime_inventory", None)
            else None
        )
        result = validate_documentation_inputs(
            payload,
            metadata_documents=metadata_documents,
            checked_examples_path=getattr(args, "checked_examples", None),
            runtime_inventory=runtime_inventory,
            accepted_exceptions=accepted_exceptions,
        )
    except DocumentationSchemaError as exc:
        print_stderr(f"{exc}\n")
        return 1

    print(f"Validated {args.json_path}")
    for gap in result.completeness_gaps:
        print(f"Completeness gap: {gap}")
    return 0


def run_report_docs(args: argparse.Namespace) -> int:
    try:
        model = load_json(args.normalized_json)
        output = quality_report(model)
    except DocumentationSchemaError as exc:
        print_stderr(f"{exc}\n")
        return 1

    out_file = args.out_file or args.normalized_json.with_name(QUALITY_REPORT_NAME)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(output, encoding="utf-8")
    print(f"Wrote documentation quality report to {out_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "check":
        return run_check(args)
    if args.command == "lint-docs":
        return run_lint_docs(args)
    if args.command == "generate-docs":
        return run_generate_docs(args)
    if args.command == "validate-docs":
        return run_validate_docs(args)
    if args.command == "report-docs":
        return run_report_docs(args)
    return run_generate(args)
