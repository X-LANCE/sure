"""SLU Judge harness bridge tests; all model calls are simulated offline."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate_predictions as evaluation  # noqa: E402
import run_eval as runner  # noqa: E402


class JudgeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = evaluation.HARNESS_ROOT / "sure" / "external" / "sure-evaluation"
        self.prompts = self.root / "questions.jsonl"
        self.prompts.write_text(json.dumps({
            "key": "a", "task": "SLU", "language": "en", "question": "Which city?",
            "choices": {"A": "Paris", "B": "London"}, "target": "A",
            "context": "The speaker says Paris.",
        }) + "\n", encoding="utf-8")
        self.prediction = self.root / "prediction.txt"
        self.prediction.write_text("a\tA, Paris.\n", encoding="utf-8")
        self.config = self.root / "judge.json"
        self.config.write_text(json.dumps({
            "base_url": "https://example.invalid/v1", "model": "offline-judge",
            "api_key": "test-only-secret", "rubric": "Score correctness against evidence from 0 to 100.",
        }), encoding="utf-8")
        fake = self.root / "fake_api"
        fake.mkdir()
        (fake / "sitecustomize.py").write_text(
            "import json\n"
            "from sure_eval.evaluation.nodes.scoring.llm_judge import node\n"
            "def judge(config, messages):\n"
            "    data = json.loads(messages[1]['content'])\n"
            "    assert data['reference_answer'] == 'A'\n"
            "    assert data['answer'] == 'A, Paris.'\n"
            "    assert data['choices']['A'] == 'Paris'\n"
            "    return {'score': float(config.instruction or 100), 'reason': 'matches reference', 'response_model': config.model}\n"
            "node.call_judge = judge\n", encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([
            str(self.engine / "src"), str(fake), os.environ.get("PYTHONPATH", ""),
        ])
        env["PYTHONIOENCODING"] = "utf-8"
        self.env = env
        self.manager = SimpleNamespace(
            normalize_dataset_name=lambda name: name, get_jsonl_path=lambda name: self.prompts,
        )
        self.sota = mock.Mock()
        self.sota.get_metric.return_value = "accuracy"
        self.sota.calculate_rps.return_value = {"rps": 1.0}

    def evaluate(self, metric="llm_judge", *, config=True, pipeline_id=None):
        with (
            mock.patch.object(evaluation, "_external_env", return_value=self.env),
            mock.patch.object(evaluation, "_evaluation_runtime_binding", return_value={"python_executable": sys.executable}),
        ):
            return evaluation.evaluate_prediction_file_external(
                self.manager, self.sota, "test__v1__slu", self.prediction,
                engine_source="submodule", engine_root=self.engine,
                external_runs_dir=self.root / "runs", device="cpu", cache_dir=None, timeout=30,
                metric_override=metric if pipeline_id is None else None, pipeline_id_override=pipeline_id,
                **({"llm_config": str(self.config)} if config else {}),
            )

    def test_real_subprocess_bridge_and_sample_reports(self):
        result = self.evaluate()
        self.assertEqual(result["score"], 100.0)
        self.assertIsNone(result["rps"])
        self.sota.calculate_rps.assert_not_called()
        self.assertEqual(result["details"]["pipeline"]["run_args"]["llm_config"], str(self.config.resolve()))
        judge = result["evaluation_context"]["judge"]
        self.assertEqual(judge["config"]["model"], "offline-judge")
        self.assertNotIn("api_key", judge["config"])
        self.assertNotIn("api_key_env", judge["config"])
        self.assertEqual(len(judge["prompt_sha256"]), 64)
        sample_report = self.root / "sample-report.jsonl"
        evaluation._write_sample_report(
            output_path=sample_report, samples=evaluation.load_jsonl(self.prompts),
            predictions=evaluation.load_prediction_map(self.prediction), result=result,
        )
        sample = json.loads(sample_report.read_text(encoding="utf-8"))
        self.assertEqual((sample["score"], sample["reason"]), (100.0, "matches reference"))
        self.assertEqual(sample["raw_score"], 100)
        row = evaluation._dataset_metric_row(result)
        standard = evaluation._standard_report_row_v1(
            row=row, validation={}, run_id="audit", protocol_id="standard_system",
            model_dir=None, tool_name="test-model",
        )
        self.assertEqual(standard["pipeline"]["judge"], judge)
        self.assertEqual(standard["metric"]["unit"], "score")
        self.assertEqual(standard["metric"]["display"], "100.00/100")
        self.assertEqual(standard["metric"]["raw_score"], 100)
        self.assertEqual(standard["metric"]["score_normalization"], "none")
        self.assertEqual(standard["metric"]["score_range"], [0, 100])
        self.assertEqual(judge["response_models"], ["offline-judge"])
        self.assertNotIn("test-only-secret", json.dumps(standard))
        self.assertNotIn("test-only-secret", json.dumps(result))

    def test_exact_pipeline_id_also_reaches_judge(self):
        result = self.evaluate(pipeline_id="slu.any.llm_judge.llm_judge_v1")
        self.assertEqual(result["metric"], "llm_judge")

    def test_empty_answer_reaches_judge_without_becoming_missing_prediction(self):
        self.prediction.write_text("a\t\n", encoding="utf-8")
        (self.root / "fake_api/sitecustomize.py").write_text(
            "import json\n"
            "from sure_eval.evaluation.nodes.scoring.llm_judge import node\n"
            "def judge(config, messages):\n"
            "    assert json.loads(messages[1]['content'])['answer'] == ''\n"
            "    return {'score': 0, 'reason': 'empty answer'}\n"
            "node.call_judge = judge\n", encoding="utf-8",
        )
        result = self.evaluate()
        self.assertEqual(result["provided_predictions"], 1)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["details"]["report"]["pipeline_trace"][0]["details"]["result"]["num_scored"], 1)
        self.sota.calculate_rps.assert_not_called()

    def test_judge_can_use_text_context_without_reference_answers(self):
        prompt = json.loads(self.prompts.read_text(encoding="utf-8"))
        prompt.pop("target")
        self.prompts.write_text(json.dumps(prompt) + "\n", encoding="utf-8")
        (self.root / "fake_api/sitecustomize.py").write_text(
            "import json\n"
            "from sure_eval.evaluation.nodes.scoring.llm_judge import node\n"
            "def judge(config, messages):\n"
            "    data = json.loads(messages[1]['content'])\n"
            "    assert 'reference_answer' not in data\n"
            "    assert 'Paris' in data['context']\n"
            "    return {'score': 80, 'reason': 'supported by text context'}\n"
            "node.call_judge = judge\n", encoding="utf-8",
        )
        result = self.evaluate()
        self.assertEqual(result["score"], 80)
        trace = result["details"]["report"]["pipeline_trace"][0]["details"]
        self.assertNotIn("ref", trace["input_files"])
        self.sota.calculate_rps.assert_not_called()

    def test_insufficient_evidence_preserves_partial_report_and_fails_bridge(self):
        original = json.loads(self.prompts.read_text(encoding="utf-8"))
        self.prompts.write_text("".join(
            json.dumps({**original, "key": key}) + "\n" for key in ("a", "b", "c")
        ), encoding="utf-8")
        self.prediction.write_text("a\tParis\nb\tUnknown\nc\tParis\n", encoding="utf-8")
        (self.root / "fake_api/sitecustomize.py").write_text(
            "import json\n"
            "from sure_eval.evaluation.nodes.scoring.llm_judge import node\n"
            "calls = 0\n"
            "def judge(config, messages):\n"
            "    global calls\n"
            "    calls += 1\n"
            "    if calls == 2:\n"
            "        return {'score': None, 'reason': 'insufficient textual evidence'}\n"
            "    return {'score': 80, 'reason': 'supported answer'}\n"
            "node.call_judge = judge\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "judge_batch_failed") as error:
            self.evaluate()
        reports = list((self.root / "runs").rglob("report.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        details = report["pipeline_trace"][0]["details"]
        self.assertIsNone(report["score"])
        self.assertEqual(details["result"]["num_scored"], 2)
        self.assertEqual(details["result"]["num_failed"], 1)
        self.assertEqual(details["sample_results"][1]["error_code"], "insufficient_evidence")
        self.assertEqual(details["sample_results"][2]["status"], "success")
        self.sota.calculate_rps.assert_not_called()
        self.assertNotIn("test-only-secret", str(error.exception))
        self.assertNotIn("test-only-secret", json.dumps(report))

    def test_native_scores_never_use_real_accuracy_baseline(self):
        baseline = self.root / "baseline.yaml"
        baseline.write_text(
            "test__v1__slu:\n  metric: accuracy\n  score: 80.0\n"
            "  higher_is_better: true\n  sota_model: fixture\n", encoding="utf-8",
        )
        self.sota = evaluation.SOTAManager(baseline)
        for raw_score in (0, 80.125, 100):
            with self.subTest(raw_score=raw_score):
                config = json.loads(self.config.read_text(encoding="utf-8"))
                config["instruction"] = str(raw_score)
                self.config.write_text(json.dumps(config), encoding="utf-8")
                result = self.evaluate()
                self.assertEqual(result["score"], raw_score)
                self.assertIsNone(result["rps"])
                self.assertEqual(result["evaluation_context"]["judge"]["raw_score"], raw_score)
                self.assertEqual(result["details"]["report"]["score"], raw_score)
                self.assertEqual(result["evaluation_context"]["judge"]["rps_baseline_policy"], "requires_matching_judge_baseline")

    def test_missing_or_other_metric_baseline_does_not_calculate_proxy(self):
        for metric in (None, "wer", "bleu"):
            with self.subTest(baseline_metric=metric):
                self.sota.get_metric.return_value = metric
                self.sota.calculate_rps.reset_mock()
                result = self.evaluate()
                self.assertEqual(result["score"], 100.0)
                self.assertIsNone(result["rps"])
                self.assertIn("No comparable Judge baseline", result["evaluation_context"]["rps_unavailable_reason"])
                self.sota.calculate_rps.assert_not_called()

    @unittest.skipUnless(os.environ.get("SURE_TEST_LOCKED_EVALUATION") == "1", "requires prepared locked runtimes")
    def test_locked_runtime_bridge_with_local_api(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(payload)
                body = json.dumps({"model": "offline-locked-judge", "choices": [{
                    "finish_reason": "stop", "message": {"content": json.dumps({"score": 100, "reason": "matches reference"})},
                }]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.config.write_text(json.dumps({
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "model": "offline-judge", "api_key": "test-only-secret", "rubric": "Score correctness against evidence from 0 to 100.",
        }), encoding="utf-8")
        try:
            result = evaluation.evaluate_prediction_file_external(
                self.manager, self.sota, "test__v1__slu", self.prediction,
                engine_source="submodule", engine_root=self.engine,
                external_runs_dir=self.root / "locked-runs", device="cpu", cache_dir=None,
                timeout=30, metric_override="llm_judge", llm_config=str(self.config),
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(len(seen), 1)
        data = json.loads(seen[0]["messages"][1]["content"])
        self.assertEqual(data["reference_answer"], "A")
        self.assertNotIn("test-only-secret", json.dumps(result))

    def test_existing_accuracy_ignores_judge_settings_and_keeps_rps(self):
        self.prediction.write_text("a\tA\n", encoding="utf-8")
        result = self.evaluate("accuracy")
        self.assertEqual(result["score"], 1.0)
        self.sota.calculate_rps.assert_called_once()
        self.assertNotIn("judge", result["evaluation_context"])
        self.assertNotIn("llm_config", result["details"]["pipeline"]["run_args"])

    def test_missing_config_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, "requires --llm-config"):
            self.evaluate(config=False)

    def test_missing_config_file_is_rejected(self):
        self.config.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "configuration file"):
            self.evaluate()

    def test_llm_config_changes_reval_identity(self):
        source = {
            "predictions": [{"dataset": "test", "txt_sha256": "same", "txt": str(self.prediction)}],
            "protocol_id": "standard_system", "model_name": "model", "model_dir": "model",
            "model_fingerprint": "same", "source_report_sha256": "same",
            "source_results_dir": str(self.root), "dataset_set_digest": "same",
        }
        path = self.root / "report.jsonl"
        ids = []
        for model in ("judge-a", "judge-b"):
            path.write_text(json.dumps({
                "dataset": {"name": "test", "jsonl_path": str(self.prompts)},
                "metric": {"name": "llm_judge"},
                "pipeline": {"pipeline_id": "same", "nodes": [], "judge": {
                    "config": {"model": model}, "rubric_sha256": "same", "prompt_sha256": "same",
                }},
            }) + "\n", encoding="utf-8")
            rows = runner._reval_report_rows(scratch_report=path, source=source, engine={})
            ids.append(rows[0]["reval"]["record_id"])
        self.assertNotEqual(*ids)

    def test_judge_score_does_not_change_reval_input_identity(self):
        source = {
            "predictions": [{"dataset": "test", "txt_sha256": "same", "txt": str(self.prediction)}],
            "protocol_id": "standard_system", "model_name": "model", "model_dir": "model",
            "model_fingerprint": "same", "source_report_sha256": "same",
            "source_results_dir": str(self.root), "dataset_set_digest": "same",
        }
        path = self.root / "report.jsonl"
        ids = []
        for score in (10, 90):
            path.write_text(json.dumps({
                "dataset": {"name": "test", "jsonl_path": str(self.prompts)},
                "metric": {"name": "llm_judge", "score": score},
                "pipeline": {"pipeline_id": "same", "nodes": [], "judge": {
                    "config": {"model": "judge"}, "rubric_sha256": "same",
                    "prompt_sha256": "same", "raw_score": score,
                }},
            }) + "\n", encoding="utf-8")
            rows = runner._reval_report_rows(scratch_report=path, source=source, engine={})
            ids.append(rows[0]["reval"]["record_id"])
        self.assertEqual(*ids)

    def test_runner_forwards_configuration_path_without_reading_secret(self):
        invocation = self.root / ".sure" / "runs" / "test"
        source = {
            "source_kind": "local_infer_run", "datasets": ["test"], "protocol_id": "standard_system",
        }
        args = argparse.Namespace(
            model="model", datasets=["test"], pipeline_id=["slu.any.llm_judge.llm_judge_v1"],
            metric=None, llm_config=str(self.config), run_id="audit", device="cpu",
            invocation_run_dir=str(invocation), protocol_id="standard_system",
        )
        captured = []

        def execute(command, **kwargs):
            if any(str(item).endswith("evaluate_predictions.py") for item in command):
                captured.extend(command)
                raise RuntimeError("captured scoring command")

        with (
            mock.patch.object(runner, "HARNESS_ROOT", self.root),
            mock.patch.object(runner, "resolve_prediction_source", return_value=source),
            mock.patch.object(runner, "resolve_engine_root", return_value=("test", self.engine)),
            mock.patch.object(runner, "_harness_config", return_value=self.root / "harness.yaml"),
            mock.patch.object(runner, "import_predictions", return_value={}),
            mock.patch.object(runner, "_ensure_ffmpeg"),
            mock.patch.object(runner, "_run", side_effect=execute),
            self.assertRaisesRegex(RuntimeError, "captured scoring command"),
        ):
            runner.run_eval(args, approved_models_root=self.root, approved_results_root=self.root)
        self.assertEqual(captured[captured.index("--llm-config") + 1], str(self.config.resolve()))
        self.assertNotIn("test-only-secret", captured)


if __name__ == "__main__":
    unittest.main()
