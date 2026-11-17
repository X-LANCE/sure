#!/usr/bin/env python3
"""Tests for the vendored SUREEvaluator scoring paths.

Run directly:
    cd sure/skills/sure_infer/scripts && python test_sure_evaluator.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sure_eval.evaluation.sure_evaluator import SUREEvaluator  # noqa: E402

STM_LINES = ["session1 1 spk1 0.0 1.0 hello world"]


def _write(directory: Path, name: str, lines: list[str]) -> str:
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _der(error_rate: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        error_rate=error_rate,
        missed_speaker_time=0.0,
        falarm_speaker_time=0.0,
        speaker_error_time=0.0,
    )


def _fake_meeteval(sessions: dict) -> types.ModuleType:
    """Stand-in for the parts of the meeteval API the evaluator uses."""
    module = types.ModuleType("meeteval")
    module.io = types.SimpleNamespace(load=lambda path: path)
    module.der = types.SimpleNamespace(dscore=lambda ref, hyp, collar=0.0: dict(sessions))
    module.wer = types.SimpleNamespace(
        cpwer=lambda ref, hyp: {},
        combine_error_rates=lambda values: types.SimpleNamespace(error_rate=0.25),
    )
    return module


def _fake_sacrebleu(calls: list) -> dict:
    """Stand-in for sacrebleu that records the corpora it is scored on."""

    class _Metric:
        def __init__(self, *args, **kwargs):
            pass

        def corpus_score(self, hypotheses, references):
            calls.append((list(hypotheses), [list(reference) for reference in references]))
            return types.SimpleNamespace(score=42.0)

    metrics = types.ModuleType("sacrebleu.metrics")
    metrics.BLEU = _Metric
    metrics.CHRF = _Metric
    package = types.ModuleType("sacrebleu")
    package.metrics = metrics
    return {"sacrebleu": package, "sacrebleu.metrics": metrics}


class MeetevalDependencyTests(unittest.TestCase):
    """A missing dependency or an empty session set is an error, not a 0.0 score."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ref = _write(self.tmp, "ref.stm", STM_LINES)
        self.hyp = _write(self.tmp, "hyp.stm", STM_LINES)
        self.evaluator = SUREEvaluator(language="en")

    def test_sd_without_meeteval_raises_naming_the_package(self):
        with mock.patch.dict(sys.modules, {"meeteval": None}):
            with self.assertRaises(RuntimeError) as ctx:
                self.evaluator._eval_sd(self.ref, self.hyp)
        self.assertIn("meeteval", str(ctx.exception))

    def test_sa_asr_without_meeteval_raises_naming_the_package(self):
        with mock.patch.dict(sys.modules, {"meeteval": None}):
            with self.assertRaises(RuntimeError) as ctx:
                self.evaluator._eval_sa_asr(self.ref, self.hyp)
        self.assertIn("meeteval", str(ctx.exception))

    def test_sd_without_sessions_raises(self):
        with mock.patch.dict(sys.modules, {"meeteval": _fake_meeteval({})}):
            with self.assertRaises(ValueError) as ctx:
                self.evaluator._eval_sd(self.ref, self.hyp)
        self.assertIn("session", str(ctx.exception).lower())

    def test_sa_asr_without_sessions_raises(self):
        with mock.patch.dict(sys.modules, {"meeteval": _fake_meeteval({})}):
            with self.assertRaises(ValueError) as ctx:
                self.evaluator._eval_sa_asr(self.ref, self.hyp)
        self.assertIn("session", str(ctx.exception).lower())

    def test_sd_scores_when_sessions_exist(self):
        with mock.patch.dict(sys.modules, {"meeteval": _fake_meeteval({"session1": _der(0.5)})}):
            result = self.evaluator._eval_sd(self.ref, self.hyp)
        self.assertEqual(result["num_sessions"], 1)
        self.assertAlmostEqual(result["der"], 0.5)

    def test_sa_asr_scores_when_sessions_exist(self):
        with mock.patch.dict(sys.modules, {"meeteval": _fake_meeteval({"session1": _der(0.5)})}):
            result = self.evaluator._eval_sa_asr(self.ref, self.hyp)
        self.assertEqual(result["num_sessions"], 1)
        self.assertAlmostEqual(result["der"], 0.5)
        self.assertAlmostEqual(result["cpwer"], 0.25)


class UnsupportedTaskTests(unittest.TestCase):
    def test_lid_names_external_backend(self) -> None:
        for task in ("LID", "language_identification"):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                ref = _write(Path(tmp), "ref.txt", ["u1\ten"])
                hyp = _write(Path(tmp), "hyp.txt", ["u1\ten"])
                with self.assertRaises(ValueError) as ctx:
                    SUREEvaluator().evaluate(task, ref, hyp)
                self.assertIn("--evaluation-backend external", str(ctx.exception))
                self.assertIn("evaluation engine", str(ctx.exception))


class KeyPairingTests(unittest.TestCase):
    """Predictions are paired with references by key, never by line number."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.evaluator = SUREEvaluator(language="en")

    def test_ser_pairs_by_key_when_a_prediction_is_absent(self):
        ref = _write(self.tmp, "ref.txt", ["a\tneutral", "b\thappy", "c\tangry"])
        hyp = _write(self.tmp, "hyp.txt", ["b\thappy", "c\tangry"])
        self.assertEqual(
            self.evaluator._eval_ser(ref, hyp),
            {"accuracy": 1.0, "score": 1.0, "missing_predictions": 1, "missing_references": 0},
        )

    def test_ser_keeps_an_empty_prediction_value_as_a_wrong_answer(self):
        ref = _write(self.tmp, "ref.txt", ["a\tneutral", "b\thappy"])
        hyp = _write(self.tmp, "hyp.txt", ["a\t", "b\thappy"])
        self.assertEqual(
            self.evaluator._eval_ser(ref, hyp),
            {"accuracy": 0.5, "score": 0.5, "missing_predictions": 0, "missing_references": 0},
        )

    def test_ser_counts_predictions_without_a_reference(self):
        ref = _write(self.tmp, "ref.txt", ["a\tneutral"])
        hyp = _write(self.tmp, "hyp.txt", ["a\tneutral", "z\tangry"])
        self.assertEqual(
            self.evaluator._eval_ser(ref, hyp),
            {"accuracy": 1.0, "score": 1.0, "missing_predictions": 0, "missing_references": 1},
        )

    def test_gr_pairs_by_key_when_a_prediction_is_absent(self):
        ref = _write(self.tmp, "ref.txt", ["a\tmale", "b\tfemale", "c\tmale"])
        hyp = _write(self.tmp, "hyp.txt", ["b\tfemale", "c\tmale"])
        self.assertEqual(
            self.evaluator._eval_gr(ref, hyp),
            {"accuracy": 1.0, "score": 1.0, "missing_predictions": 1, "missing_references": 0},
        )

    def test_gr_keeps_an_empty_prediction_value_as_a_wrong_answer(self):
        ref = _write(self.tmp, "ref.txt", ["a\tmale", "b\tfemale"])
        hyp = _write(self.tmp, "hyp.txt", ["a\t", "b\tfemale"])
        self.assertEqual(
            self.evaluator._eval_gr(ref, hyp),
            {"accuracy": 0.5, "score": 0.5, "missing_predictions": 0, "missing_references": 0},
        )

    def test_s2tt_scores_the_keys_present_on_both_sides(self):
        ref = _write(self.tmp, "ref.txt", ["a\tguten tag", "b\thallo welt", "c\tbis bald"])
        hyp = _write(self.tmp, "hyp.txt", ["b\thallo welt", "c\tbis bald"])
        calls: list = []
        with mock.patch.dict(sys.modules, _fake_sacrebleu(calls)):
            result = self.evaluator._eval_s2tt(ref, hyp)
        self.assertEqual(calls[0], (["hallo welt", "bis bald"], [["hallo welt", "bis bald"]]))
        self.assertEqual(result["missing_predictions"], 1)
        self.assertEqual(result["missing_references"], 0)

    def test_s2tt_keeps_an_empty_prediction_value(self):
        ref = _write(self.tmp, "ref.txt", ["a\tguten tag", "b\thallo welt"])
        hyp = _write(self.tmp, "hyp.txt", ["a\t", "b\thallo welt"])
        calls: list = []
        with mock.patch.dict(sys.modules, _fake_sacrebleu(calls)):
            result = self.evaluator._eval_s2tt(ref, hyp)
        self.assertEqual(calls[0], (["", "hallo welt"], [["guten tag", "hallo welt"]]))
        self.assertEqual(result["missing_predictions"], 0)

    def test_asr_counts_an_empty_prediction_as_deletions(self):
        ref = _write(self.tmp, "ref.txt", ["u1\thello world", "u2\tgood morning everyone"])
        hyp = _write(self.tmp, "hyp.txt", ["u1\thello world", "u2\t"])
        result = self.evaluator.evaluate("asr", ref, hyp)
        self.assertEqual((result["all"], result["del"]), (5, 3))
        self.assertAlmostEqual(result["wer"], 0.6)

    def test_codeswitch_asr_counts_an_empty_prediction_as_deletions(self):
        ref = _write(self.tmp, "ref.txt", ["u1\thello world", "u2\tgood morning everyone"])
        hyp = _write(self.tmp, "hyp.txt", ["u1\thello world", "u2\t"])
        result = SUREEvaluator(language="cs").evaluate("asr", ref, hyp)
        self.assertEqual((result["mer_details"]["all"], result["mer_details"]["del"]), (5, 3))
        self.assertAlmostEqual(result["mer"], 0.6)

    def test_slu_keeps_an_empty_prediction_value_as_a_wrong_answer(self):
        prompts = self.tmp / "prompts.jsonl"
        prompts.write_text(
            "\n".join(
                json.dumps({"key": key, "prompt": f"What does the speaker want?\nA. {a}\nB. {b}"})
                for key, a, b in [("u1", "book a flight", "cancel a flight"), ("u2", "play music", "stop music")]
            )
            + "\n",
            encoding="utf-8",
        )
        ref = _write(self.tmp, "ref.txt", ["u1\tB", "u2\tA"])
        hyp = _write(self.tmp, "hyp.txt", ["u1\tB", "u2\t"])
        self.assertEqual(self.evaluator.evaluate("slu", ref, hyp, prompt_jsonl=str(prompts)), 0.5)


class AsrSymbolRuleTests(unittest.TestCase):
    """Punctuation the recogniser leaves out must not become words in the reference."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def score(self, ref_text: str, hyp_text: str) -> dict:
        ref = _write(self.tmp, "ref.txt", [f"u1\t{ref_text}"])
        hyp = _write(self.tmp, "hyp.txt", [f"u1\t{hyp_text}"])
        return SUREEvaluator(language="en").evaluate("asr", ref, hyp)

    def test_quotation_marks_are_not_read_as_inches(self):
        result = self.score('he said "hello" to me', "he said hello to me")
        self.assertEqual((result["all"], result["wer"]), (5, 0.0))

    def test_a_slash_separates_words_without_becoming_one(self):
        result = self.score("we need both and/or rules", "we need both and or rules")
        self.assertEqual((result["all"], result["wer"]), (6, 0.0))

    def test_other_marks_are_still_dropped_inside_a_word(self):
        for ref, hyp, words in (
            ("it's well-known", "its wellknown", 2),
            ("the U.S. team", "the US team", 3),
            ("e.g. this", "eg this", 2),
        ):
            with self.subTest(ref=ref):
                result = self.score(ref, hyp)
                self.assertEqual((result["all"], result["wer"]), (words, 0.0))


if __name__ == "__main__":
    unittest.main()
