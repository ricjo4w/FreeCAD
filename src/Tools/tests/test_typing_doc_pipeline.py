# SPDX-License-Identifier: LGPL-2.1-or-later

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "Tools" / "typing" / "generate_stubs.py"


FIXTURE_SOURCE = """\
static PyObject* hello(PyObject*, PyObject*) { return nullptr; }
static PyObject* needs_docs(PyObject*, PyObject*) { return nullptr; }

static PyMethodDef FreeCAD_methods[] = {
    {"hello", hello, METH_NOARGS, "Say hello."},
    {"needs_docs", needs_docs, METH_VARARGS, nullptr},
    {nullptr, nullptr, 0, nullptr}
};

static PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "FreeCAD",
    nullptr,
    -1,
    FreeCAD_methods
};

PyObject* mod = PyModule_Create(&moduledef);
"""


class TypingDocumentationPipelineCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def write_fixture(self, root: Path) -> None:
        source = root / "src" / "App" / "Fixture.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(FIXTURE_SOURCE, encoding="utf-8")

    def write_public_stubs(self, root: Path) -> Path:
        stubs_dir = root / "src" / "Tools" / "typing" / "generated"
        stubs_dir.mkdir(parents=True)
        (stubs_dir / "FreeCAD.pyi").write_text(
            """\
class Document:
    def recompute(self, force: bool = False) -> None: ...

class SparseType:
    ...

def openDocument(path: str) -> Document: ...
def sparse(name): ...
""",
            encoding="utf-8",
        )
        (stubs_dir / "Draft.pyi").write_text("", encoding="utf-8")
        return stubs_dir

    def write_normalized_json(self, root: Path) -> Path:
        path = root / "normalized-documentation.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "freecad-doc-normalized-v1",
                    "metadata": {"generator": "test", "source": "test"},
                    "modules": [
                        {
                            "name": "FreeCAD",
                            "summary": "",
                            "completeness": "partial",
                            "source_surface": "public-stub",
                            "source": {"path": "FreeCAD.pyi", "line": 1},
                            "members": [
                                {
                                    "name": "Document",
                                    "qualified_name": "FreeCAD.Document",
                                    "kind": "class",
                                    "signature": "",
                                    "summary": "",
                                    "doc": "",
                                    "completeness": "typed",
                                    "source_surface": "public-stub",
                                    "source": {"path": "FreeCAD.pyi", "line": 1},
                                },
                                {
                                    "name": "openDocument",
                                    "qualified_name": "FreeCAD.openDocument",
                                    "kind": "function",
                                    "signature": "(path: str)",
                                    "summary": "",
                                    "doc": "",
                                    "completeness": "typed",
                                    "source_surface": "public-stub",
                                    "source": {"path": "FreeCAD.pyi", "line": 2},
                                },
                            ],
                        },
                        {
                            "name": "Draft",
                            "summary": "",
                            "completeness": "discovered",
                            "source_surface": "public-stub",
                            "source": {"path": "Draft.pyi", "line": 1},
                            "members": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_agent_index_json(self, root: Path) -> Path:
        path = root / "agent-api-index.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "freecad-agent-api-index-v1",
                    "source_schema_version": "freecad-doc-normalized-v1",
                    "symbols": [
                        {
                            "qualified_name": "FreeCAD.openDocument",
                            "kind": "function",
                            "signature": "(path: str)",
                            "summary": "",
                            "completeness": "typed",
                            "source_surface": "public-stub",
                            "source": {"path": "FreeCAD.pyi", "line": 2},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_metadata(self, root: Path, payload: dict) -> Path:
        path = root / "metadata.yaml"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_generate_docs_writes_model_index_rst_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_fixture(root)
            out_dir = root / "docs-out"

            result = self.run_cli(
                "generate-docs",
                "--root",
                str(root),
                "--source-dir",
                "src",
                "--out-dir",
                str(out_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized_path = out_dir / "normalized-documentation.json"
            index_path = out_dir / "agent-api-index.json"
            rst_index_path = out_dir / "sphinx" / "python_api" / "index.rst"
            report_path = out_dir / "documentation-quality-report.md"

            self.assertTrue(normalized_path.exists())
            self.assertTrue(index_path.exists())
            self.assertTrue(rst_index_path.exists())
            self.assertTrue(report_path.exists())

            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            self.assertEqual(normalized["schema_version"], "freecad-doc-normalized-v1")
            self.assertEqual(normalized["modules"][0]["name"], "FreeCAD")
            self.assertEqual(
                [member["name"] for member in normalized["modules"][0]["members"]],
                ["hello", "needs_docs"],
            )

            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["schema_version"], "freecad-agent-api-index-v1")
            self.assertEqual(
                [symbol["qualified_name"] for symbol in index["symbols"]],
                ["FreeCAD.hello", "FreeCAD.needs_docs"],
            )

            self.assertIn("FreeCAD", rst_index_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Completeness State Counts", report)
            self.assertIn("`documented`: 1", report)
            self.assertIn("`missing`: 1", report)

    def test_generate_docs_inventories_public_stub_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            stubs_dir = self.write_public_stubs(root)
            out_dir = root / "docs-out"

            result = self.run_cli(
                "generate-docs",
                "--root",
                str(root),
                "--source-dir",
                "src",
                "--stubs-dir",
                str(stubs_dir),
                "--out-dir",
                str(out_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(
                (out_dir / "normalized-documentation.json").read_text(encoding="utf-8")
            )
            modules = {module["name"]: module for module in normalized["modules"]}
            self.assertEqual(modules["Draft"]["source_surface"], "public-stub")
            self.assertEqual(modules["Draft"]["completeness"], "discovered")
            freecad_members = {
                member["qualified_name"]: member for member in modules["FreeCAD"]["members"]
            }
            self.assertEqual(
                freecad_members["FreeCAD.openDocument"]["source_surface"], "public-stub"
            )
            self.assertEqual(freecad_members["FreeCAD.openDocument"]["completeness"], "typed")
            self.assertEqual(freecad_members["FreeCAD.sparse"]["completeness"], "discovered")
            self.assertEqual(freecad_members["FreeCAD.Document"]["kind"], "class")
            self.assertEqual(freecad_members["FreeCAD.Document.recompute"]["kind"], "method")
            self.assertEqual(
                freecad_members["FreeCAD.Document.recompute"]["signature"],
                "(self, force: bool=False) -> None",
            )

            index = json.loads((out_dir / "agent-api-index.json").read_text(encoding="utf-8"))
            self.assertIn(
                "FreeCAD.Document.recompute",
                [symbol["qualified_name"] for symbol in index["symbols"]],
            )
            self.assertEqual(
                next(
                    symbol
                    for symbol in index["symbols"]
                    if symbol["qualified_name"] == "FreeCAD.openDocument"
                )["source_surface"],
                "public-stub",
            )

            rst_index = (out_dir / "sphinx" / "python_api" / "index.rst").read_text(
                encoding="utf-8"
            )
            rst_freecad = (out_dir / "sphinx" / "python_api" / "FreeCAD.rst").read_text(
                encoding="utf-8"
            )
            self.assertIn("Draft", rst_index)
            self.assertIn("FreeCAD.Document.recompute", rst_freecad)
            self.assertIn("Source surface: ``public-stub``", rst_freecad)

            report = (out_dir / "documentation-quality-report.md").read_text(encoding="utf-8")
            self.assertIn("Public Stub Surface Counts", report)
            self.assertIn("`module:discovered`: 1", report)
            self.assertIn("`module:partial`: 1", report)
            self.assertIn("`symbol:typed`: 2", report)
            self.assertIn("`symbol:discovered`: 3", report)

    def test_validate_docs_returns_nonzero_for_schema_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid_json = Path(tmp) / "invalid.json"
            invalid_json.write_text('{"schema_version": "wrong"}\n', encoding="utf-8")

            result = self.run_cli("validate-docs", str(invalid_json))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown schema_version", result.stderr)

    def test_validate_docs_fails_on_invalid_curated_yaml_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = root / "metadata.yaml"
            metadata.write_text("symbols: [", encoding="utf-8")

            result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid", result.stderr)

    def test_validate_docs_fails_on_invalid_normalized_completeness_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            payload = json.loads(normalized.read_text(encoding="utf-8"))
            payload["modules"][0]["members"][0]["completeness"] = "aspirational"
            normalized.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("validate-docs", str(normalized))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid completeness value", result.stderr)

    def test_validate_docs_fails_on_invalid_agent_api_index_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = self.write_agent_index_json(root)
            payload = json.loads(index.read_text(encoding="utf-8"))
            payload["symbols"][0]["source"] = "FreeCAD.pyi"
            index.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("validate-docs", str(index))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbols[0].source must be an object", result.stderr)

    def test_validate_docs_fails_on_broken_metadata_symbol_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_metadata(
                root,
                {
                    "symbols": [
                        {
                            "qualified_name": "FreeCAD.missing",
                            "completeness": "typed",
                        }
                    ]
                },
            )

            result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("references unknown symbol", result.stderr)

    def test_validate_docs_fails_on_broken_example_object_and_workbench_references(self):
        cases = [
            (
                {
                    "examples": [
                        {"path": "bad.py", "symbols": ["FreeCAD.missing"], "required": False}
                    ]
                },
                "examples[0].symbols references unknown symbol",
            ),
            (
                {
                    "object_types": [
                        {"name": "FreeCAD.Document", "symbols": ["FreeCAD.missing"]}
                    ]
                },
                "object_types[0].symbols references unknown symbol",
            ),
            (
                {
                    "workbenches": [
                        {"name": "Draft", "modules": ["MissingWorkbenchModule"]}
                    ]
                },
                "workbenches[0].modules references unknown module",
            ),
            (
                {
                    "symbols": [
                        {
                            "qualified_name": "FreeCAD.openDocument",
                            "completeness": "typed",
                            "examples": ["missing.py"],
                        }
                    ]
                },
                "symbols[0].examples references unknown example",
            ),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                normalized = self.write_normalized_json(root)
                metadata = self.write_metadata(root, payload)

                result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_validate_docs_fails_on_malformed_completeness_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_metadata(
                root,
                {
                    "completeness": [
                        {"qualified_name": "FreeCAD.openDocument", "state": "almost"}
                    ]
                },
            )

            result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid completeness value", result.stderr)

    def test_validate_docs_fails_when_required_checked_example_is_missing_or_invalid(self):
        cases = [
            ({}, "required example is missing"),
            ({"required.py": "def broken(:\n    pass\n"}, "failed syntax check"),
        ]
        for files, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                normalized = self.write_normalized_json(root)
                examples_dir = root / "examples"
                examples_dir.mkdir()
                for name, content in files.items():
                    (examples_dir / name).write_text(content, encoding="utf-8")
                metadata = self.write_metadata(
                    root,
                    {
                        "examples": [
                            {
                                "path": "required.py",
                                "symbols": ["FreeCAD.openDocument"],
                                "required": True,
                                "checks": ["syntax"],
                            }
                        ]
                    },
                )

                result = self.run_cli(
                    "validate-docs",
                    str(normalized),
                    "--metadata",
                    str(metadata),
                    "--checked-examples",
                    str(examples_dir),
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_validate_docs_fails_on_runtime_conflict_without_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            runtime_inventory = root / "runtime.json"
            runtime_inventory.write_text(
                json.dumps(
                    {
                        "symbols": [
                            {
                                "qualified_name": "FreeCAD.openDocument",
                                "kind": "class",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "validate-docs",
                str(normalized),
                "--runtime-inventory",
                str(runtime_inventory),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime kind 'class' conflicts", result.stderr)

    def test_validate_docs_accepts_runtime_conflict_with_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            runtime_inventory = root / "runtime.json"
            runtime_inventory.write_text(
                json.dumps(
                    {
                        "symbols": [
                            {
                                "qualified_name": "FreeCAD.openDocument",
                                "kind": "class",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            exceptions = root / "exceptions.yaml"
            exceptions.write_text(
                json.dumps(
                    {
                        "runtime_exceptions": [
                            {
                                "qualified_name": "FreeCAD.openDocument",
                                "runtime_kind": "class",
                                "documented_kind": "function",
                                "reason": "Runtime exposes a factory object.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "validate-docs",
                str(normalized),
                "--runtime-inventory",
                str(runtime_inventory),
                "--accepted-exceptions",
                str(exceptions),
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_validate_docs_reports_completeness_gaps_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_metadata(
                root,
                {
                    "completeness": [
                        {
                            "qualified_name": "FreeCAD.openDocument",
                            "state": "missing",
                            "reason": "Docs still need examples.",
                        }
                    ]
                },
            )

            result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Completeness gap: FreeCAD.openDocument: missing", result.stdout)

    def test_report_docs_writes_completeness_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_fixture(root)
            out_dir = root / "docs-out"
            generated = self.run_cli(
                "generate-docs",
                "--root",
                str(root),
                "--source-dir",
                "src",
                "--out-dir",
                str(out_dir),
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            report_path = root / "report.md"
            result = self.run_cli(
                "report-docs",
                str(out_dir / "normalized-documentation.json"),
                "--out-file",
                str(report_path),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("`documented`: 1", report)
            self.assertIn("`missing`: 1", report)


if __name__ == "__main__":
    unittest.main()
