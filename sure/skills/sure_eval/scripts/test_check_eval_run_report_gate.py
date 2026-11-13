#!/usr/bin/env python3
"""Gate-level tests for check_eval_run_report.py without a live site policy.

The full validate() needs an approved model root, an invocation-local run
directory and the pinned evaluation-engine checkout; those are integration
concerns. These tests cover the parts that decide whether a fake or escaped
artifact can pass at all: result-relative path resolution, symlink rejection,
canonical hashing, and the order in which structural errors are reported.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_eval_run_report as gate  # noqa: E402


class ResultPathSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.result_dir = self.tmp / "result"
        self.result_dir.mkdir()

    def test_rejects_absolute_paths(self) -> None:
        with self.assertRaises(ValueError):
            gate._resolve_result_path(str(self.tmp / "escape.json"), self.result_dir)

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            gate._resolve_result_path("../escape.json", self.result_dir)

    def test_rejects_empty_path(self) -> None:
        with self.assertRaises(ValueError):
            gate._resolve_result_path("", self.result_dir)

    def test_accepts_result_relative_path(self) -> None:
        resolved = gate._resolve_result_path("evaluation_runs/batch/report.json", self.result_dir)
        self.assertTrue(str(resolved).startswith(str(self.result_dir.resolve())))

    def test_rejects_a_symlinked_artifact_tree(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.result_dir / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        with self.assertRaises(ValueError):
            gate._regular_files(self.result_dir)


class CanonicalJsonTests(unittest.TestCase):
    def test_key_order_does_not_change_the_digest(self) -> None:
        self.assertEqual(gate._canonical_json({"b": 1, "a": 2}), gate._canonical_json({"a": 2, "b": 1}))


class ValidateOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.approved = self.tmp / "approved"
        self.approved.mkdir()

    def _policy(self) -> dict:
        return {
            "policy": {
                "storage": {
                    "approved_models_roots": [str(self.approved)],
                    "approved_results_roots": [],
                }
            }
        }

    def test_schema_and_flags_are_reported_before_artifact_checks(self) -> None:
        report = self.tmp / "eval_run_report.json"
        report.write_text(
            json.dumps({"schema": "wrong", "evaluation_only": False, "old_evaluation_reused": True}),
            encoding="utf-8",
        )
        with mock.patch.object(gate, "load_site_policy", return_value=self._policy()):
            errors = gate.validate(report)
        self.assertIn("schema must be sure.eval.run_report.v1", errors)
        self.assertIn("evaluation_only must be true", errors)
        self.assertIn("old_evaluation_reused must be false", errors)
        self.assertTrue(any("missing artifact keys" in error for error in errors))

    def test_invalid_json_is_reported_not_raised(self) -> None:
        report = self.tmp / "broken.json"
        report.write_text("{not json", encoding="utf-8")
        with mock.patch.object(gate, "load_site_policy", return_value=self._policy()):
            errors = gate.validate(report)
        self.assertTrue(errors)

    def test_main_returns_nonzero_for_a_missing_report(self) -> None:
        missing = self.tmp / "missing.json"
        with mock.patch.object(sys, "argv", ["check_eval_run_report.py", "--report", str(missing)]):
            self.assertEqual(gate.main(), 1)

    def test_main_returns_nonzero_for_an_invalid_report(self) -> None:
        report = self.tmp / "invalid.json"
        report.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        with mock.patch.object(gate, "load_site_policy", return_value=self._policy()):
            with mock.patch.object(sys, "argv", ["check_eval_run_report.py", "--report", str(report)]):
                self.assertEqual(gate.main(), 1)


if __name__ == "__main__":
    unittest.main()
