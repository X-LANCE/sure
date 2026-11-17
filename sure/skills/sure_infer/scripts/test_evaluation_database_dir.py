#!/usr/bin/env python3
"""The legacy evaluation database must not touch the filesystem until it writes.

Constructing it used to create the database directory, so every process that
built an `RPSManager` left an empty `results/` behind in whatever directory it
happened to be started from.

Run directly:
    cd sure/skills/sure_infer/scripts && python test_evaluation_database_dir.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate_predictions  # noqa: E402
from sure_eval.evaluation import rps  # noqa: E402


class EvaluationDatabaseDirTests(unittest.TestCase):
    def test_construction_does_not_create_the_database_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "results" / "evaluations.json"
            rps.EvaluationDatabase(db_path)
            self.assertFalse(db_path.parent.exists())

    def test_saving_a_record_creates_the_database_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "results" / "evaluations.json"
            database = rps.EvaluationDatabase(db_path)
            record = rps.EvaluationRecord(
                tool_name="demo_tool",
                model_name="demo_model",
                dataset="demo_dataset",
                metric="cer",
                score=1.0,
                rps=None,
            )
            with mock.patch.object(rps, "logger"):
                database.add_record(record)
            self.assertTrue(db_path.is_file())


def _record(tool_name: str) -> rps.EvaluationRecord:
    return rps.EvaluationRecord(
        tool_name=tool_name, model_name=None, dataset="demo_dataset", metric="cer", score=1.0, rps=None
    )


class EvaluationDatabaseHistoryTests(unittest.TestCase):
    """A bad or interrupted write must not cost the records already on disk."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "evaluations.json"
        patcher = mock.patch.object(rps, "logger")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_truncated_file_is_kept_when_a_new_record_is_added(self) -> None:
        database = rps.EvaluationDatabase(self.db_path)
        database.add_record(_record("old_tool"))
        original = self.db_path.read_bytes()
        truncated = original[: len(original) // 2]
        self.db_path.write_bytes(truncated)

        rps.EvaluationDatabase(self.db_path).add_record(_record("new_tool"))

        kept = [p.read_bytes() for p in self.db_path.parent.iterdir() if p != self.db_path]
        self.assertIn(truncated, kept)
        self.assertEqual([r.tool_name for r in rps.EvaluationDatabase(self.db_path).get_records()], ["new_tool"])

    def test_a_failed_write_leaves_the_previous_file_intact(self) -> None:
        database = rps.EvaluationDatabase(self.db_path)
        database.add_record(_record("old_tool"))
        original = self.db_path.read_bytes()

        with mock.patch.object(rps.json, "dump", side_effect=OSError("disk full")):
            database.add_record(_record("new_tool"))

        self.assertEqual(self.db_path.read_bytes(), original)


class RecordedRpsTests(unittest.TestCase):
    """--record stores the RPS the run reported, not a second calculation."""

    def test_record_keeps_the_reported_rps(self) -> None:
        reported = {
            "dataset": "demo_ds__v1__asr", "metric": "cer", "score": 10.0, "rps": 0.8,
            "num_samples": 1, "prediction_path": "demo.txt", "details": {}, "evaluation_context": {},
        }
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            prediction = Path(temporary) / "demo.txt"
            prediction.write_text("s1\thello\n", encoding="utf-8")
            config = Path(temporary) / "harness.yaml"
            config.write_text("{}\n", encoding="utf-8")
            # the database lands in ./results, so run from the temporary directory
            stack.callback(os.chdir, os.getcwd())
            os.chdir(temporary)
            stack.enter_context(mock.patch.object(evaluate_predictions, "_peek_dataset_task_language", return_value=("ASR", "zh")))
            stack.enter_context(mock.patch.object(evaluate_predictions, "evaluate_prediction_file", return_value=reported))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(mock.patch.object(sys, "argv", [
                "evaluate_predictions.py", "--dataset", "demo_ds__v1__asr", "--pred", "demo_ds__v1__asr", str(prediction),
                "--config", str(config), "--evaluation-backend", "legacy", "--record", "--tool-name", "demo_tool",
            ]))
            evaluate_predictions.main()
            records = rps.EvaluationDatabase(Path(temporary) / "results" / "evaluations.json").get_records()
        self.assertEqual([(r.tool_name, r.rps) for r in records], [("demo_tool", 0.8)])


if __name__ == "__main__":
    unittest.main()
