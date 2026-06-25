# SPDX-License-Identifier: LGPL-2.1-or-later

import json
import os
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
    def addObject(self, type_id: str, name: str) -> DocumentObject: ...
    def getObject(self, name: str) -> DocumentObject | None: ...
    def recompute(self, force: bool = False) -> None: ...

class DocumentObject:
    Name: str
    Label: str

class SparseType:
    ...

def newDocument(name: str) -> Document: ...
def openDocument(path: str) -> Document: ...
def sparse(name): ...
""",
            encoding="utf-8",
        )
        (stubs_dir / "Draft.pyi").write_text("", encoding="utf-8")
        return stubs_dir

    def write_sketcher_public_stubs(self, root: Path) -> Path:
        stubs_dir = self.write_public_stubs(root)
        (stubs_dir / "Part.pyi").write_text(
            """\
class LineSegment:
    ...

class Circle:
    ...
""",
            encoding="utf-8",
        )
        (stubs_dir / "Sketcher.pyi").write_text(
            """\
class Constraint:
    Type: str
    Value: float

class SketchObject:
    Name: str
    Geometry: list[object]
    Constraints: list[Constraint]
    def addGeometry(self, geometry: object, construction: bool = False) -> int: ...
    def addConstraint(self, constraint: Constraint) -> int: ...
""",
            encoding="utf-8",
        )
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

    def write_fake_freecad_cmd(self, root: Path) -> Path:
        path = root / "fake-freecad-cmd.py"
        path.write_text(
            """\
#!/usr/bin/env python3
import runpy
import sys
import types


class DocumentObject:
    def __init__(self, name):
        self.Name = name
        self.Label = name
        self.PropertiesList = ["Name", "Label"]


class SketchObject(DocumentObject):
    def __init__(self, name):
        super().__init__(name)
        self.Geometry = []
        self.Constraints = []

    def addGeometry(self, geometry, construction=False):
        self.Geometry.append(geometry)
        return len(self.Geometry) - 1

    def addConstraint(self, constraint):
        self.Constraints.append(constraint)
        return len(self.Constraints) - 1


class Document:
    def __init__(self, name):
        self.Name = name
        self._objects = {}

    def addObject(self, type_id, name):
        obj = SketchObject(name) if type_id == "Sketcher::SketchObject" else DocumentObject(name)
        self._objects[name] = obj
        return obj

    def getObject(self, name):
        return self._objects.get(name)

    def recompute(self):
        return 1


def newDocument(name):
    return Document(name)


class Vector:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class LineSegment:
    def __init__(self, start, end):
        self.StartPoint = start
        self.EndPoint = end


class Circle:
    def __init__(self, center, axis, radius):
        self.Center = center
        self.Axis = axis
        self.Radius = radius


class Constraint:
    def __init__(self, type_name, *args):
        self.Type = type_name
        self.Args = args
        self.Value = args[-1] if args and isinstance(args[-1], float) else None


sys.modules["FreeCAD"] = types.SimpleNamespace(newDocument=newDocument, Vector=Vector)
sys.modules["Part"] = types.SimpleNamespace(LineSegment=LineSegment, Circle=Circle)
sys.modules["Sketcher"] = types.SimpleNamespace(Constraint=Constraint)
script_args = sys.argv[1:]
sys.argv = script_args
runpy.run_path(script_args[0], run_name="__main__")
""",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | os.X_OK)
        return path

    def write_document_api_availability_metadata(self, root: Path) -> Path:
        return self.write_metadata(
            root,
            {
                "document_api": {
                    "objects": [
                        {
                            "name": "Part::Box",
                            "properties": ["Name", "Label", "Length"],
                            "completeness": "documented",
                        }
                    ],
                    "properties": [
                        {
                            "name": "Name",
                            "object_type": "Part::Box",
                            "completeness": "documented",
                        },
                        {
                            "name": "Label",
                            "object_type": "Part::Box",
                            "completeness": "documented",
                        },
                        {
                            "name": "Length",
                            "object_type": "Part::Box",
                            "completeness": "documented",
                        },
                    ],
                }
            },
        )

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
            self.assertIn("`symbol:typed`: 5", report)
            self.assertIn("`symbol:discovered`: 4", report)

    def test_generate_docs_merges_document_api_tracer_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            stubs_dir = self.write_public_stubs(root)
            metadata = root / "document_api_tracer.json"
            examples_dir = root / "examples"
            examples_dir.mkdir()
            (examples_dir / "document_api_tracer.py").write_text(
                (REPO_ROOT / "Tools" / "typing" / "docs" / "examples" / "document_api_tracer.py")
                .read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            metadata.write_text(
                (
                    REPO_ROOT
                    / "Tools"
                    / "typing"
                    / "docs"
                    / "metadata"
                    / "document_api_tracer.json"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            out_dir = root / "docs-out"

            result = self.run_cli(
                "generate-docs",
                "--root",
                str(root),
                "--source-dir",
                "src",
                "--stubs-dir",
                str(stubs_dir),
                "--metadata",
                str(metadata),
                "--checked-examples",
                str(examples_dir),
                "--out-dir",
                str(out_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(
                (out_dir / "normalized-documentation.json").read_text(encoding="utf-8")
            )
            document_api = normalized["document_api"]
            self.assertEqual(document_api["modules"][0]["name"], "FreeCAD")
            self.assertIn(
                "FreeCAD.Document.addObject",
                [record["qualified_name"] for record in document_api["symbols"]],
            )
            self.assertIn(
                "Part::Box",
                [record["name"] for record in document_api["objects"]],
            )
            self.assertIn(
                "Label",
                [record["name"] for record in document_api["properties"]],
            )
            self.assertEqual(document_api["examples"][0]["path"], "document_api_tracer.py")
            self.assertEqual(
                {
                    record["name"]
                    for record in document_api["anti_patterns"]
                },
                {
                    "gui-command-reliance",
                    "skipped-recompute",
                    "label-name-confusion",
                    "dynamic-properties-too-early",
                },
            )

            index = json.loads((out_dir / "agent-api-index.json").read_text(encoding="utf-8"))
            self.assertEqual(
                next(
                    record
                    for record in index["document_api"]["symbols"]
                    if record["qualified_name"] == "FreeCAD.Document.recompute"
                )["completeness"],
                "documented",
            )

            rst = (
                out_dir / "sphinx" / "python_api" / "document-api-tracer-path.rst"
            ).read_text(encoding="utf-8")
            self.assertIn("Document API Tracer Path", rst)
            self.assertIn("document_api_tracer.py", rst)
            self.assertIn("skipped-recompute", rst)

            report = (out_dir / "documentation-quality-report.md").read_text(encoding="utf-8")
            self.assertIn("Document API Status", report)
            self.assertIn("Status: `present`", report)
            self.assertIn("Missing docs: 0", report)
            self.assertIn("Missing examples: 0", report)
            self.assertIn("`anti_patterns:documented`: 4", report)

            validate = self.run_cli(
                "validate-docs",
                str(out_dir / "normalized-documentation.json"),
                "--metadata",
                str(metadata),
                "--checked-examples",
                str(examples_dir),
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)

            runtime_validate = self.run_cli(
                "validate-docs",
                str(out_dir / "normalized-documentation.json"),
                "--metadata",
                str(metadata),
                "--checked-examples",
                str(examples_dir),
                "--freecad-executable",
                str(self.write_fake_freecad_cmd(root)),
            )
            self.assertEqual(runtime_validate.returncode, 0, runtime_validate.stderr)

    def test_generate_docs_merges_sketcher_headless_workflow_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            stubs_dir = self.write_sketcher_public_stubs(root)
            metadata = root / "sketcher_headless_workflow.json"
            examples_dir = root / "examples"
            examples_dir.mkdir()
            (examples_dir / "sketcher_headless_workflow.py").write_text(
                (
                    REPO_ROOT
                    / "Tools"
                    / "typing"
                    / "docs"
                    / "examples"
                    / "sketcher_headless_workflow.py"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            metadata.write_text(
                (
                    REPO_ROOT
                    / "Tools"
                    / "typing"
                    / "docs"
                    / "metadata"
                    / "sketcher_headless_workflow.json"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            out_dir = root / "docs-out"

            result = self.run_cli(
                "generate-docs",
                "--root",
                str(root),
                "--source-dir",
                "src",
                "--stubs-dir",
                str(stubs_dir),
                "--metadata",
                str(metadata),
                "--checked-examples",
                str(examples_dir),
                "--freecad-executable",
                str(self.write_fake_freecad_cmd(root)),
                "--out-dir",
                str(out_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(
                (out_dir / "normalized-documentation.json").read_text(encoding="utf-8")
            )
            sketcher = normalized["sketcher"]
            self.assertIn("Sketcher", [record["name"] for record in sketcher["modules"]])
            self.assertIn(
                "Sketcher.SketchObject.addGeometry",
                [record["qualified_name"] for record in sketcher["symbols"]],
            )
            self.assertIn(
                "Sketcher::SketchObject",
                [record["name"] for record in sketcher["objects"]],
            )
            self.assertIn("Geometry", [record["name"] for record in sketcher["properties"]])
            self.assertEqual(sketcher["examples"][0]["path"], "sketcher_headless_workflow.py")
            self.assertIn(
                "Sketcher Workbench Guide",
                [record["name"] for record in sketcher["workbenches"]],
            )
            self.assertIn(
                "sketcher-skipped-recompute",
                [record["name"] for record in sketcher["anti_patterns"]],
            )

            index = json.loads((out_dir / "agent-api-index.json").read_text(encoding="utf-8"))
            self.assertIn(
                "Sketcher.Constraint",
                [record["qualified_name"] for record in index["sketcher"]["symbols"]],
            )

            rst = (out_dir / "sphinx" / "python_api" / "sketcher-reference.rst").read_text(
                encoding="utf-8"
            )
            self.assertIn("Sketcher Reference", rst)
            self.assertIn("Sketcher Workbench Guide", rst)
            self.assertIn("FreeCAD.Document.recompute", rst)

            report = (out_dir / "documentation-quality-report.md").read_text(encoding="utf-8")
            self.assertIn("Sketcher Status", report)
            self.assertIn("Status: `present`", report)
            self.assertIn("Missing docs: 0", report)
            self.assertIn("Missing examples: 0", report)
            self.assertIn("`workbenches:documented`: 1", report)

            validate = self.run_cli(
                "validate-docs",
                str(out_dir / "normalized-documentation.json"),
                "--metadata",
                str(metadata),
                "--checked-examples",
                str(examples_dir),
                "--freecad-executable",
                str(self.write_fake_freecad_cmd(root)),
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)

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

    def test_validate_docs_fails_when_required_checked_example_has_no_examples_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_metadata(
                root,
                {
                    "examples": [
                        {
                            "path": "missing.py",
                            "symbols": ["FreeCAD.openDocument"],
                            "required": True,
                            "checks": ["syntax"],
                        }
                    ]
                },
            )

            result = self.run_cli("validate-docs", str(normalized), "--metadata", str(metadata))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required example needs --checked-examples", result.stderr)

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

    def test_validate_docs_fails_on_runtime_property_absence_without_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_document_api_availability_metadata(root)
            runtime_inventory = root / "runtime.json"
            runtime_inventory.write_text(
                json.dumps(
                    {
                        "schema_version": "freecad-doc-runtime-inventory-v1",
                        "document_api": {
                            "objects": [
                                {
                                    "name": "Part::Box",
                                    "available": True,
                                    "properties": [
                                        {"name": "Name", "available": True},
                                        {"name": "Length", "available": False},
                                    ],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "validate-docs",
                str(normalized),
                "--metadata",
                str(metadata),
                "--runtime-inventory",
                str(runtime_inventory),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Part::Box.Length is unavailable", result.stderr)

    def test_validate_docs_accepts_runtime_property_absence_with_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = self.write_normalized_json(root)
            metadata = self.write_document_api_availability_metadata(root)
            runtime_inventory = root / "runtime.json"
            runtime_inventory.write_text(
                json.dumps(
                    {
                        "schema_version": "freecad-doc-runtime-inventory-v1",
                        "document_api": {
                            "objects": [
                                {
                                    "name": "Part::Box",
                                    "available": True,
                                    "properties": [
                                        {"name": "Length", "available": False},
                                    ],
                                }
                            ]
                        },
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
                                "object_type": "Part::Box",
                                "property_name": "Length",
                                "available": False,
                                "reason": "Runtime fixture intentionally omits Length.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_cli(
                "validate-docs",
                str(normalized),
                "--metadata",
                str(metadata),
                "--runtime-inventory",
                str(runtime_inventory),
                "--accepted-exceptions",
                str(exceptions),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Availability note: Document API runtime availability: property "
                "Part::Box.Length is unavailable",
                result.stdout,
            )

    def test_runtime_inventory_collects_document_api_object_property_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = self.write_document_api_availability_metadata(root)
            out_file = root / "runtime.json"

            result = self.run_cli(
                "runtime-inventory",
                "--metadata",
                str(metadata),
                "--out-file",
                str(out_file),
                "--freecad-executable",
                str(self.write_fake_freecad_cmd(root)),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            inventory = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(
                inventory["schema_version"],
                "freecad-doc-runtime-inventory-v1",
            )
            box = inventory["document_api"]["objects"][0]
            self.assertEqual(box["name"], "Part::Box")
            self.assertTrue(box["available"])
            properties = {record["name"]: record for record in box["properties"]}
            self.assertTrue(properties["Name"]["available"])
            self.assertTrue(properties["Label"]["available"])
            self.assertFalse(properties["Length"]["available"])

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
