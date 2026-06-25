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

    def test_validate_docs_returns_nonzero_for_schema_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid_json = Path(tmp) / "invalid.json"
            invalid_json.write_text('{"schema_version": "wrong"}\n', encoding="utf-8")

            result = self.run_cli("validate-docs", str(invalid_json))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown schema_version", result.stderr)

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
