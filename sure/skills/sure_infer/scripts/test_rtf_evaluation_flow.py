#!/usr/bin/env python3
"""Regression coverage for the harness-side streaming-RTF evaluation route."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate_predictions as evaluation  # noqa: E402

PIPELINE_ID = "asr.en.rtf.streaming_rtf_v1"


class _EvaluationManager:
    def __init__(self, reference: Path) -> None:
        self.reference = reference

    def normalize_dataset_name(self, name: str) -> str:
        return name

    def get_jsonl_path(self, _name: str) -> Path:
        return self.reference


class _SotaManager:
    def get_metric(self, _name: str) -> str:
        return "wer"

    def calculate_rps(self, _name: str, _score: float, **_kwargs: object) -> float:
        return 1.0


def _timing_row(key: str, *, chunk_ms: int = 960, audio_samples: int = 48000) -> dict[str, object]:
    return {
        "key": key,
        "schema_version": 1,
        "audio_samples": audio_samples,
        "audio_seconds": audio_samples / 16000,
        "chunk_ms_model": chunk_ms,
        "feed_ms": 100,
        "num_threads": 1,
        "t_accept_s": 0.00003,
        "t_decode_s": 0.42426,
        "total_compute_s": 0.42429,
        "num_encoder_chunks": 2,
        "chunk_compute_s": [0.2121, 0.21216],
        "tail_padding_s": 1.5,
        "warm": True,
        "provider": "cpu",
    }


class RtfEvaluationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "references" / "librispeech.jsonl"
        self.reference.parent.mkdir()
        samples = [
            {"key": "a", "task": "ASR", "language": "en", "text": "one"},
            {"key": "b", "task": "ASR", "language": "en", "text": "two"},
        ]
        self.reference.write_text(
            "".join(json.dumps(row) + "\n" for row in samples), encoding="utf-8"
        )
        self.predictions = self.root / "predictions" / "librispeech.txt"
        self.predictions.parent.mkdir()
        self.predictions.write_text("a\tone\nb\ttwo\n", encoding="utf-8")
        prediction_rows = [
            {
                "key": key,
                "task": "ASR",
                "language": "en",
                "prediction": {"text": text},
                "raw_response": {"text": text, "timing": _timing_row(key)},
            }
            for key, text in (("a", "one"), ("b", "two"))
        ]
        self.predictions.with_suffix(".jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in prediction_rows), encoding="utf-8"
        )

    def _pipeline(self) -> dict[str, object]:
        return {
            "pipeline_id": PIPELINE_ID,
            "metric": "rtf",
            "required_roles": ["timing_jsonl"],
            "nodes": [{"node_id": "scoring/streaming_rtf"}],
        }

    def test_dispatch_selects_the_timing_route(self) -> None:
        self.assertTrue(evaluation._pipeline_uses_timing_jsonl(self._pipeline()))
        self.assertFalse(
            evaluation._pipeline_uses_timing_jsonl({"required_roles": ["hyp", "ref"], "run_args": {}})
        )

    def test_bridge_projects_recorded_timings_into_the_sidecar(self) -> None:
        captured: dict[str, object] = {}

        def run_external(*, request: dict[str, object], **_kwargs: object) -> dict[str, object]:
            captured.update(request)
            sidecar = Path(str(request["timing_jsonl"]))
            rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["key"] for row in rows], ["a", "b"])
            self.assertEqual({row["chunk_ms_model"] for row in rows}, {960})
            self.assertEqual(rows[0]["total_compute_s"], 0.42429)
            return {
                "summary": {
                    "metric": "rtf",
                    "score": 0.1414,
                    "pipeline_id": PIPELINE_ID,
                    "language": "en",
                    "output_dir": str(request["output_dir"]),
                },
                "pipeline": self._pipeline(),
                "report": {"score": 0.1414},
            }

        with (
            mock.patch.object(evaluation, "_describe_external_pipeline", return_value=self._pipeline()),
            mock.patch.object(evaluation, "_run_external_pipeline", side_effect=run_external),
            mock.patch.object(
                evaluation, "_evaluation_runtime_binding", return_value={"runtime_id": "test"}
            ),
        ):
            result = evaluation.evaluate_rtf_prediction_file_external(
                _EvaluationManager(self.reference),
                _SotaManager(),
                "librispeech_test_clean_v1__v1__asr",
                self.predictions,
                engine_source="submodule",
                engine_root=Path("/engine"),
                external_runs_dir=self.root / "external",
                device="cpu",
                cache_dir=None,
                timeout=30,
                metric_override="rtf",
                task_override="ASR",
            )

        self.assertEqual(captured["pipeline_id"], None)
        self.assertEqual(captured["metric"], "rtf")
        self.assertEqual(result["metric"], "rtf")
        self.assertEqual(result["score"], 0.1414)
        self.assertEqual(result["pipeline_id"], PIPELINE_ID)
        self.assertEqual(result["num_samples"], 2)
        self.assertIsNone(result["rps"])
        self.assertEqual(result["evaluation_context"]["timing_source"], "reused_prediction_rows")
        self.assertEqual(result["evaluation_context"]["input_roles"], ["timing_jsonl"])

    def test_bridge_refuses_predictions_without_recorded_timings(self) -> None:
        structured = self.predictions.with_suffix(".jsonl")
        rows = [json.loads(line) for line in structured.read_text(encoding="utf-8").splitlines()]
        rows[1]["raw_response"].pop("timing")
        structured.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

        with (
            mock.patch.object(evaluation, "_describe_external_pipeline", return_value=self._pipeline()),
            mock.patch.object(evaluation, "_run_external_pipeline") as run_external,
            mock.patch.object(
                evaluation, "_evaluation_runtime_binding", return_value={"runtime_id": "test"}
            ),
        ):
            with self.assertRaisesRegex(ValueError, "carries no recorded wrapper timing"):
                evaluation.evaluate_rtf_prediction_file_external(
                    _EvaluationManager(self.reference),
                    _SotaManager(),
                    "librispeech_test_clean_v1__v1__asr",
                    self.predictions,
                    engine_source="submodule",
                    engine_root=Path("/engine"),
                    external_runs_dir=self.root / "external",
                    device="cpu",
                    cache_dir=None,
                    timeout=30,
                    metric_override="rtf",
                    task_override="ASR",
                )
        run_external.assert_not_called()


if __name__ == "__main__":
    unittest.main()
