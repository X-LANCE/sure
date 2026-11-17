#!/usr/bin/env python3
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sure_feed.providers.huggingface as hf_module  # noqa: E402
import sure_feed.providers.base as base_module  # noqa: E402
import sure_feed.providers.modelscope as modelscope_module  # noqa: E402
import sure_feed.fixture_registry as fixture_registry_module  # noqa: E402
import sure_feed.modelscope_watcher as modelscope_watcher_module  # noqa: E402
import check_model_input as check_model_input_module  # noqa: E402
import sure_feed_online_discover as discover_module  # noqa: E402
import xforge_modelscope_fetch as fetch_module  # noqa: E402
import xforge_watch_modelscope as watch_module  # noqa: E402
from sure_feed.catalog import XForgeCatalog  # noqa: E402
from sure_feed.fixture_registry import select_fixture_for_task  # noqa: E402
from sure_feed.providers.base import ProviderNetworkError, ProviderRequest, infer_task, synthesize_model_input, task_defaults, to_yaml  # noqa: E402
from sure_feed.providers.github import GitHubProvider  # noqa: E402
from sure_feed.providers.huggingface import HuggingFaceProvider  # noqa: E402
from sure_feed.providers.modelscope import ModelScopeProvider  # noqa: E402
from sure_feed_online_discover import parse_model_url  # noqa: E402
from sure.runtime.evaluation.task_registry import canonical_tasks  # noqa: E402


WSL_LAUNCHER = r"C:\Windows\System32\bash.exe"


def usable_bash() -> str | None:
    try:
        return fetch_module._find_bash()
    except fetch_module.BridgeError:
        return None


class ProviderTests(unittest.TestCase):
    def sample_readme(self) -> str:
        return """---
library_name: sample_tts
---
```bash
conda create -n sample_tts python=3.10 -y
python -m pip install "git+https://github.com/acme-lab/sample-tts.git"
```
```python
from sample_tts.runtime import SampleTtsRuntime
import soundfile as sf

runtime = SampleTtsRuntime.from_pretrained(
    "acme-lab/sample-tts-base",
    precision="bfloat16",
)

result = runtime.generate(
    text="Hello, this is a quick speech synthesis test.",
    prompt_audio_path="/path/to/reference.wav",
    prompt_text="The exact transcript of the reference audio.",
)

sf.write("output.wav", result["audio"].float().cpu().numpy(), result["sample_rate"])
```
        """

    def test_kws_task_defaults_include_keywords_in_infer_test(self) -> None:
        defaults = task_defaults("kws")

        self.assertEqual(defaults["io_contract"]["input_type"], "audio_path_with_keywords")
        self.assertIn("<keywords from fixture>", defaults["infer_test"])

    def test_huggingface_auto_falls_back_to_mirror_and_keeps_canonical_repo(self) -> None:
        original = hf_module.http_get_json
        original_text = hf_module.http_get_text
        calls: list[str] = []

        def fake_get_json(url: str, headers=None, timeout: int = 20):
            calls.append(url)
            if url.startswith(hf_module.PRIMARY_ENDPOINT):
                raise ProviderNetworkError("primary endpoint down")
            return [
                {
                    "modelId": "owner/asr-model",
                    "pipeline_tag": "automatic-speech-recognition",
                    "tags": ["license:apache-2.0"],
                    "downloads": 7,
                }
            ]

        def fake_get_text(url: str, headers=None, timeout: int = 20, max_bytes: int = 100_000):
            if url.startswith(hf_module.PRIMARY_ENDPOINT):
                return ""
            return self.sample_readme()

        hf_module.http_get_json = fake_get_json
        hf_module.http_get_text = fake_get_text
        try:
            provider = HuggingFaceProvider(endpoint_mode="auto")
            candidates = provider.search(ProviderRequest(source="huggingface", query="asr", task="asr", max_models=1))
        finally:
            hf_module.http_get_json = original
            hf_module.http_get_text = original_text

        self.assertEqual(len(candidates), 1)
        self.assertEqual(provider.last_endpoint_used, hf_module.MIRROR_ENDPOINT)
        self.assertIn("primary endpoint down", provider.fallback_reason or "")
        self.assertEqual(candidates[0]["repo"], "https://huggingface.co/owner/asr-model")
        self.assertEqual(candidates[0]["endpoint_used"], hf_module.MIRROR_ENDPOINT)

    def test_huggingface_direct_normalizes_tts_url(self) -> None:
        original = hf_module.http_get_json
        original_text = hf_module.http_get_text

        def fake_get_json(url: str, headers=None, timeout: int = 20):
            if url.startswith(hf_module.PRIMARY_ENDPOINT):
                raise ProviderNetworkError("primary endpoint down")
            return {
                "modelId": "acme-lab/sample-tts-base",
                "pipeline_tag": "text-to-speech",
                "library_name": "sample_tts",
                "sha": "abc123",
                "tags": ["text-to-speech", "tts", "license:apache-2.0"],
                "downloads": 710,
            }

        def fake_get_text(url: str, headers=None, timeout: int = 20, max_bytes: int = 100_000):
            if url.startswith(hf_module.PRIMARY_ENDPOINT):
                return ""
            return self.sample_readme()

        hf_module.http_get_json = fake_get_json
        hf_module.http_get_text = fake_get_text
        try:
            candidate = HuggingFaceProvider(endpoint_mode="auto").direct("acme-lab/sample-tts-base")
        finally:
            hf_module.http_get_json = original
            hf_module.http_get_text = original_text

        matched, task_type, score, evidence, match_source = infer_task(candidate, "auto")
        self.assertTrue(matched)
        self.assertEqual(task_type, "tts")
        self.assertEqual(match_source, "pipeline_tag")
        self.assertGreaterEqual(score, 0.9)
        self.assertEqual(candidate["repo"], "https://huggingface.co/acme-lab/sample-tts-base")
        self.assertIn("SampleTtsRuntime", candidate["model_card_text"])

    def test_synthesizes_entrypoints_from_retrieved_model_card(self) -> None:
        model_input, weak_fields, evidence = synthesize_model_input(
            {
                "source": "huggingface",
                "model_id": "acme-lab/sample-tts-base",
                "repo": "https://huggingface.co/acme-lab/sample-tts-base",
                "commit": "abc123",
                "weights_source": "huggingface",
                "library_name": "sample_tts",
                "model_card_url": "https://huggingface.co/acme-lab/sample-tts-base/blob/main/README.md",
                "model_card_text": self.sample_readme(),
            },
            "tts",
        )
        self.assertEqual(weak_fields, [])
        self.assertEqual(model_input["repo"]["commit"], "abc123")
        self.assertIn("from sample_tts.runtime import SampleTtsRuntime", model_input["entrypoints"]["import_test"])
        self.assertIn("SampleTtsRuntime.from_pretrained", model_input["entrypoints"]["load_test"])
        self.assertIn("runtime.generate", model_input["entrypoints"]["infer_test"])
        self.assertEqual(model_input["environment_hint"]["preferred_backend"], "conda")
        self.assertEqual(model_input["environment_hint"]["python_version"], "3.10")
        self.assertEqual(model_input["fixture"]["fixture_source"], "task_registry")
        self.assertEqual(model_input["fixture"]["fixture_index"], "fixtures/tasks/tts/README.md")
        self.assertTrue(model_input["fixture"]["reference_audio"].startswith("fixtures/tasks/tts/"))
        covered = {item.get("model_input_field") for item in evidence}
        self.assertIn("entrypoints.import_test", covered)
        self.assertIn("io_contract", covered)
        self.assertIn("fixture", covered)

    def test_policy_defaults_python_version_when_model_card_omits_it(self) -> None:
        readme = """---
library_name: voxcpm
---
GPU inference is recommended.

```bash
pip install voxcpm
```
```python
from voxcpm import VoxCPM

model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
wav = model.generate(
    text="VoxCPM2 brings multilingual support.",
    cfg_value=2.0,
    inference_timesteps=10,
)
```
"""
        model_input, weak_fields, evidence = synthesize_model_input(
            {
                "source": "huggingface",
                "model_id": "openbmb/VoxCPM2",
                "repo": "https://huggingface.co/openbmb/VoxCPM2",
                "commit": "abc123",
                "weights_source": "huggingface",
                "library_name": "voxcpm",
                "model_card_url": "https://huggingface.co/openbmb/VoxCPM2/blob/main/README.md",
                "model_card_text": readme,
            },
            "tts",
        )
        self.assertEqual(weak_fields, [])
        self.assertEqual(model_input["environment_hint"]["python_version"], "3.10")
        self.assertEqual(model_input["environment_hint"]["python_version_source"], "sure_policy_default")
        self.assertIn("from voxcpm import VoxCPM", model_input["entrypoints"]["import_test"])
        self.assertIn("VoxCPM.from_pretrained", model_input["entrypoints"]["load_test"])
        self.assertIn("model.generate", model_input["entrypoints"]["infer_test"])
        self.assertTrue(
            any(
                item.get("source") == "local"
                and item.get("field") == "sure_policy.python_version_default"
                and item.get("model_input_field") == "environment_hint.python_version"
                for item in evidence
            )
        )

    def test_runtime_strategy_handles_sherpa_onnx_without_standard_import_load_split(self) -> None:
        readme = """# X-ASR

This model provides streaming and offline CPU ASR checkpoints for sherpa-onnx.

```bash
python -m venv .venv
pip install sherpa-onnx
```
"""
        model_input, weak_fields, evidence = synthesize_model_input(
            {
                "source": "huggingface",
                "model_id": "GilgameshWind/X-ASR-zh-en",
                "repo": "https://huggingface.co/GilgameshWind/X-ASR-zh-en",
                "commit": "abc123",
                "weights_source": "huggingface",
                "pipeline_tag": "automatic-speech-recognition",
                "tags": [
                    "sherpa-onnx",
                    "onnx",
                    "x-asr-zipformer-transducer",
                    "automatic-speech-recognition",
                ],
                "model_card_url": "https://huggingface.co/GilgameshWind/X-ASR-zh-en/blob/main/README.md",
                "model_card_text": readme,
            },
            "asr",
        )
        self.assertEqual(weak_fields, [])
        self.assertEqual(model_input["runtime_strategy"]["framework"], "sherpa-onnx")
        self.assertTrue(model_input["entrypoints"]["import_test"].startswith("policy_resolved:"))
        self.assertTrue(model_input["entrypoints"]["load_test"].startswith("policy_resolved:"))
        self.assertTrue(model_input["entrypoints"]["infer_test"].startswith("policy_resolved:"))
        self.assertNotIn("python -m venv", model_input["entrypoints"]["infer_test"])
        self.assertTrue(model_input["io_contract"]["allow_empty_prediction"])
        self.assertNotIn("text", model_input["io_contract"]["nonempty_fields"])
        self.assertEqual(
            check_model_input_module.validate_model_input(model_input, model_input["model_id"], "item"),
            [],
        )
        covered = {item.get("model_input_field") for item in evidence}
        self.assertIn("runtime_strategy", covered)
        self.assertIn("entrypoints.import_test", covered)
        self.assertIn("entrypoints.load_test", covered)
        self.assertIn("entrypoints.infer_test", covered)

    def test_fixture_registry_selects_task_specific_asr_fixture(self) -> None:
        fixture, io_contract, issues, evidence = select_fixture_for_task(
            "asr",
            {"model_id": "owner/asr", "description": "English automatic speech recognition"},
        )
        self.assertEqual(issues, [])
        self.assertIsNotNone(fixture)
        assert fixture is not None
        self.assertEqual(fixture["fixture_source"], "task_registry")
        self.assertEqual(fixture["fixture_index"], "fixtures/tasks/asr/README.md")
        self.assertIn("fixtures/tasks/asr/qwen3_asr_smoke/asr_en/", fixture["audio"])
        self.assertEqual(io_contract["primary_field"], "text")
        self.assertTrue(io_contract["allow_empty_prediction"])
        self.assertIn("fixture", {item.get("model_input_field") for item in evidence})

    def test_missing_official_fixture_is_a_resumable_resolution_request(self) -> None:
        contract = {
            "input_type": "audio_path",
            "output_type": "text",
            "primary_field": "text",
            "required_fields": ["text"],
            "nonempty_fields": ["text"],
            "json_serializable": True,
        }
        with mock.patch.object(
            base_module,
            "select_fixture_for_task",
            return_value=(None, contract, ["missing:fixture.samples.demo"], []),
        ):
            model_input, weak_fields, evidence = synthesize_model_input(
                {
                    "source": "huggingface",
                    "model_id": "owner/demo-tts",
                    "repo": "https://huggingface.co/owner/demo-tts",
                    "model_card_text": self.sample_readme(),
                },
                "tts",
            )
        self.assertEqual(model_input["fixture"]["fixture_source"], "unresolved")
        self.assertEqual(model_input["fixture"]["fixture_status"], "needs_input")
        self.assertEqual(model_input["fixture"]["resolution_options"], ["model_specific", "web_temporary"])
        self.assertIn("pending:fixture", weak_fields)
        self.assertNotIn("missing:fixture", weak_fields)
        self.assertIn("fixture", {item.get("model_input_field") for item in evidence})
        self.assertEqual(
            check_model_input_module.validate_model_input(model_input, model_input["model_id"], "item"),
            [],
        )

    def test_fixture_registry_routes_speech_understanding_to_atomic_fixtures(self) -> None:
        fixture, io_contract, issues, _evidence = select_fixture_for_task(
            "speech_understanding",
            {"model_id": "owner/transcribe-diarize", "description": "Transcription with speaker diarization"},
        )
        self.assertEqual(issues, [])
        self.assertIsNotNone(fixture)
        assert fixture is not None
        self.assertEqual(fixture["fixture_source"], "task_registry")
        self.assertEqual(fixture["fixture_index"], "fixtures/tasks/speech_understanding/README.md")
        self.assertEqual(fixture["selected_subtasks"], list(canonical_tasks()))
        self.assertEqual(len(fixture["subtask_fixtures"]), len(canonical_tasks()))
        self.assertEqual(set(fixture["subtask_io_contracts"]), set(canonical_tasks()))
        self.assertEqual(io_contract["primary_field"], "text")

    def test_speech_understanding_does_not_hide_a_missing_subtask_fixture(self) -> None:
        original = fixture_registry_module.select_atomic_fixture

        def missing_vad(task, *args, **kwargs):
            if task == "vad":
                return None, original(task, *args, **kwargs)[1], ["missing:fixture.samples.vad"]
            return original(task, *args, **kwargs)

        with mock.patch.object(fixture_registry_module, "select_atomic_fixture", side_effect=missing_vad):
            fixture, _contract, issues, _evidence = select_fixture_for_task("speech_understanding")
        self.assertIsNone(fixture)
        self.assertIn("missing:fixture.samples.vad", issues)

    def test_fixture_registry_selects_every_engine_task(self) -> None:
        for task in canonical_tasks():
            with self.subTest(task=task):
                fixture, contract, issues, _evidence = select_fixture_for_task(task)
                self.assertEqual(issues, [])
                self.assertIsNotNone(fixture)
                assert fixture is not None
                self.assertEqual(fixture["fixture_source"], "task_registry")
                self.assertTrue(fixture["fixture_root"].startswith(f"fixtures/tasks/{task}/"))
                self.assertTrue(contract["primary_field"])

    def test_sv_fixture_includes_engine_trial_manifest(self) -> None:
        fixture, contract, issues, _evidence = select_fixture_for_task("sv")
        self.assertEqual(issues, [])
        assert fixture is not None
        self.assertEqual(
            fixture["trial_manifest"],
            "fixtures/tasks/sv/librispeech_trials_smoke/trial_manifest.json",
        )
        self.assertEqual(contract["primary_field"], "embedding")

    def test_kws_contract_uses_wrapper_prediction_not_reference_label(self) -> None:
        _fixture, contract, issues, _evidence = select_fixture_for_task("kws")
        self.assertEqual(issues, [])
        self.assertEqual(contract["primary_field"], "detected")

    def test_fixture_registry_selects_sd_and_sa_asr_jsonl_fixtures(self) -> None:
        sd_fixture, sd_contract, sd_issues, _sd_evidence = select_fixture_for_task("sd", {"description": "speaker diarization"})
        self.assertEqual(sd_issues, [])
        self.assertIsNotNone(sd_fixture)
        assert sd_fixture is not None
        self.assertEqual(sd_fixture["gt"], "fixtures/tasks/sd/librispeech_2spk_smoke/gt.jsonl")
        self.assertEqual(sd_fixture["audio"], "fixtures/tasks/sd/librispeech_2spk_smoke/librispeech_2spk_001.wav")
        self.assertEqual(sd_fixture["samples"][0]["speakers"], ["spk1", "spk2"])
        self.assertEqual(sd_contract["primary_field"], "segments")

        sa_fixture, sa_contract, sa_issues, _sa_evidence = select_fixture_for_task(
            "sa_asr",
            {"description": "speaker attributed ASR"},
        )
        self.assertEqual(sa_issues, [])
        self.assertIsNotNone(sa_fixture)
        assert sa_fixture is not None
        self.assertEqual(sa_fixture["gt"], "fixtures/tasks/sa_asr/librispeech_2spk_smoke/gt.jsonl")
        self.assertEqual(sa_fixture["audio"], "fixtures/tasks/sa_asr/librispeech_2spk_smoke/librispeech_2spk_001.wav")
        self.assertEqual(sa_fixture["samples"][0]["speakers"], ["spk1", "spk2"])
        self.assertIn("text", sa_fixture["samples"][0]["segments"][0])
        self.assertEqual(sa_contract["primary_field"], "segments")

    def test_parse_direct_urls(self) -> None:
        self.assertEqual(
            parse_model_url("https://huggingface.co/acme-lab/sample-tts-base")["model_id"],
            "acme-lab/sample-tts-base",
        )
        self.assertEqual(
            parse_model_url("https://hf-mirror.com/acme-lab/sample-tts-base")["canonical_url"],
            "https://huggingface.co/acme-lab/sample-tts-base",
        )
        self.assertEqual(
            parse_model_url("https://modelscope.cn/models/owner/name")["source"],
            "modelscope",
        )
        self.assertEqual(parse_model_url("https://github.com/owner/repo")["model_id"], "owner/repo")

    def test_search_query_becomes_the_task_only_when_it_names_a_registered_task(self) -> None:
        def discovered_task(query: str) -> str:
            seen: list[str] = []

            def fake_discover(args):
                seen.append(args.task)
                return [], []

            with tempfile.TemporaryDirectory() as run_dir, \
                    mock.patch.object(discover_module, "discover", fake_discover), \
                    mock.patch.object(sys, "argv", ["sure_feed_online_discover.py", query, "--run-dir", run_dir]), \
                    mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(discover_module.main(), 3)
            return seen[0]

        self.assertEqual(discovered_task("whisper"), "auto")
        self.assertEqual(discovered_task("tts"), "tts")

    def test_modelscope_outage_is_a_provider_network_error(self) -> None:
        class HtmlResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        request = ProviderRequest(source="modelscope", query="paraformer", task="asr", max_models=3)
        for failure in (
            urllib.error.URLError("[Errno 11001] getaddrinfo failed"),
            TimeoutError("The read operation timed out"),
            HtmlResponse(b"<html>503 Service Temporarily Unavailable</html>"),
        ):
            effect = {"return_value": failure} if isinstance(failure, HtmlResponse) else {"side_effect": failure}
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(modelscope_watcher_module.urllib.request, "urlopen", **effect):
                with self.assertRaises(ProviderNetworkError):
                    ModelScopeProvider().search(request)

    def test_modelscope_direct_url_keeps_the_api_hit(self) -> None:
        class JsonResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        payload = {
            "data": {
                "models": [
                    {
                        "id": "iic/SenseVoiceSmall",
                        "name": "SenseVoiceSmall",
                        "tasks": ["auto-speech-recognition"],
                        "description": "Multilingual speech foundation model.",
                        "downloads": 1200,
                        "last_modified": "2025-01-01T00:00:00Z",
                    }
                ]
            }
        }

        def fake_urlopen(url, timeout=30):
            return JsonResponse(json.dumps(payload).encode("utf-8"))

        with mock.patch.object(modelscope_watcher_module.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(modelscope_module, "http_get_text", return_value=""):
            candidate = ModelScopeProvider().direct("iic/SenseVoiceSmall")

        self.assertEqual(candidate["tasks"], ["auto-speech-recognition"])
        self.assertEqual(candidate["download_count"], 1200)

    def test_modelscope_watch_manifests_name_a_single_task(self) -> None:
        untagged = modelscope_watcher_module._normalize_candidate({"id": "iic/SenseVoiceSmall"}, "model", "asr")
        listed = modelscope_watcher_module._normalize_candidate(
            {"id": "acme/speech-corpus", "tasks": ["auto-speech-recognition"]}, "dataset", "asr"
        )
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            modelscope_watcher_module.process_candidates(
                [untagged, listed],
                catalog=XForgeCatalog(out / "catalog.json"),
                manifest_dir=out / "manifests",
                handoff_dir=out / "handoff",
                emit_manifests=True,
                emit_handoff=False,
            )
            model = json.loads((out / "manifests" / "iic__SenseVoiceSmall.model.json").read_text(encoding="utf-8"))
            dataset = json.loads((out / "manifests" / "acme__speech-corpus.dataset.json").read_text(encoding="utf-8"))

        self.assertEqual(model["task_type"], "asr")
        self.assertEqual(dataset["task"], "AUTO-SPEECH-RECOGNITION")

    def test_modelscope_watch_caps_each_resource_type_at_max_items(self) -> None:
        class JsonResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        served = iter(range(1000))

        def fake_urlopen(url, timeout=30):
            kind = "corpus" if "/datasets?" in url else "paraformer"
            items = [
                {"id": f"acme/{kind}-{next(served)}", "tasks": ["auto-speech-recognition"]}
                for _ in range(2)
            ]
            return JsonResponse(json.dumps({"data": {"models": items}}).encode("utf-8"))

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(modelscope_watcher_module.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(sys, "argv", [
                    "xforge_watch_modelscope.py", "--task", "ASR", "--resource", "all", "--max-items", "2",
                    "--catalog", str(Path(root) / "catalog.json"),
                    "--manifest-dir", str(Path(root) / "manifests"),
                    "--handoff-dir", str(Path(root) / "handoff"),
                    "--summary", str(Path(root) / "summary.json"),
                ]), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(watch_module.main(), 0)
            summary = json.loads((Path(root) / "summary.json").read_text(encoding="utf-8"))

        manifests = [Path(item["path"]).name for item in summary["manifests"]]
        self.assertEqual(sum(name.endswith(".model.json") for name in manifests), 2)
        self.assertEqual(sum(name.endswith(".dataset.json") for name in manifests), 2)

    @unittest.skipUnless(usable_bash(), "no bash that can run the scripts here")
    def test_local_uv_bootstrap_runs_the_shell_scripts_through_bash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            model_dir = Path(root)
            for name in ("local_uv_setup.sh", "local_uv_validate.sh"):
                script = model_dir / name
                script.write_text(f"#!/usr/bin/env bash\necho ok > {name}.done\n", encoding="utf-8", newline="\n")
                script.chmod(script.stat().st_mode | 0o111)

            result = fetch_module._run_local_uv_bootstrap(model_dir)

            self.assertEqual((result["setup_returncode"], result["validate_returncode"]), (0, 0))
            self.assertTrue((model_dir / "local_uv_setup.sh.done").is_file())
            self.assertTrue((model_dir / "local_uv_validate.sh.done").is_file())

    def test_modelscope_fetch_without_usable_bash_fails_before_downloading(self) -> None:
        for platform, bash, expected in ((sys.platform, None, "bash"), ("win32", WSL_LAUNCHER, "WSL launcher")):
            with self.subTest(platform=platform, bash=bash), tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(fetch_module.sys, "platform", platform), \
                    mock.patch("shutil.which", lambda name: bash if name == "bash" else None), \
                    mock.patch.dict(os.environ, {"ProgramFiles": root}), \
                    mock.patch.object(fetch_module, "collect_remote_model_source") as download, \
                    mock.patch.object(sys, "argv", [
                        "xforge_modelscope_fetch.py", "--resource", "model", "--task", "asr", "--id", "iic/SenseVoiceSmall",
                        "--manifest-dir", str(Path(root) / "manifests"),
                        "--handoff-dir", str(Path(root) / "handoff"),
                        "--sure-plan-dir", str(Path(root) / "plans"),
                        "--fetch-run-dir", str(Path(root) / "runs"),
                        "--model-root", str(Path(root) / "models"),
                    ]), \
                    mock.patch("sys.stderr", io.StringIO()) as stderr:
                self.assertEqual(fetch_module.main(), 2)
                download.assert_not_called()
                self.assertIn(expected, stderr.getvalue())

    def test_local_uv_bootstrap_prefers_git_bash_over_the_wsl_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(fetch_module.sys, "platform", "win32"), \
                mock.patch("shutil.which", lambda name: WSL_LAUNCHER if name == "bash" else None), \
                mock.patch.dict(os.environ, {"ProgramFiles": root}):
            git_bash = Path(root) / "Git" / "bin" / "bash.exe"
            git_bash.parent.mkdir(parents=True)
            git_bash.write_bytes(b"")
            self.assertEqual(fetch_module._find_bash(), str(git_bash))

    def test_dataset_to_oref_resolves_csv_audio_next_to_the_csv(self) -> None:
        hub = types.ModuleType("modelscope.hub.snapshot_download")
        hub.snapshot_download = None
        with mock.patch.dict(sys.modules, {
            "modelscope": types.ModuleType("modelscope"),
            "modelscope.hub": types.ModuleType("modelscope.hub"),
            "modelscope.hub.snapshot_download": hub,
        }):
            oref_module = importlib.import_module("xforge_modelscope_dataset_to_oref")

        with tempfile.TemporaryDirectory() as root:
            snapshot = Path(root) / "snapshot"
            (snapshot / "wav").mkdir(parents=True)
            (snapshot / "wav" / "utt1.wav").write_bytes(b"RIFF0000WAVE")
            (snapshot / "metadata.csv").write_text("Input:FILE,Info:FILE\nwav/utt1.wav,hello world\n", encoding="utf-8")
            with mock.patch.object(oref_module, "snapshot_download", return_value=str(snapshot)), \
                    mock.patch.object(sys, "argv", [
                        "xforge_modelscope_dataset_to_oref.py", "--id", "acme/demo-asr", "--task", "asr",
                        "--work-root", str(Path(root) / "work"),
                    ]), \
                    mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()) as stderr:
                code = oref_module.main()

            self.assertEqual(code, 0, stderr.getvalue())
            copied = list((Path(root) / "work").rglob("utt1.wav"))
            self.assertTrue(copied, "audio was not materialised into the OREF layout")

    def test_hyphenated_github_topic_matches_asr(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "github",
                "model_id": "owner/repo",
                "repo": "https://github.com/owner/repo",
                "tags": ["speech-recognition"],
            },
            "asr",
        )
        self.assertTrue(matched)
        self.assertEqual(task_type, "asr")
        self.assertGreater(score, 0)
        self.assertEqual(match_source, "tags")
        self.assertEqual(evidence[0]["value"], "speech-recognition")

    def test_language_identification_model_matches_lid(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "modelscope",
                "model_id": "FireRedTeam/FireRedLID",
                "repo": "https://modelscope.cn/models/FireRedTeam/FireRedLID",
                "description": "A spoken language identification model.",
            },
            "lid",
        )
        self.assertTrue(matched)
        self.assertEqual(task_type, "lid")
        self.assertGreater(score, 0)
        self.assertEqual(match_source, "description")
        self.assertTrue(evidence)

    def test_lid_abbreviation_does_not_match_inside_unrelated_words(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "modelscope",
                "model_id": "owner/solid-audio-model",
                "repo": "https://modelscope.cn/models/owner/solid-audio-model",
                "description": "A valid uploaded audio model.",
            },
            "lid",
        )
        self.assertFalse(matched)
        self.assertEqual(task_type, "lid")
        self.assertEqual(score, 0.0)
        self.assertEqual(evidence, [])
        self.assertEqual(match_source, "")

    def test_broad_audio_text_pipeline_is_narrowed_to_sa_asr_by_research_evidence(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "huggingface",
                "model_id": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
                "repo": "https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize",
                "pipeline_tag": "audio-text-to-text",
                "tasks": ["audio-text-to-text"],
                "tags": ["asr", "diarization", "timestamp-asr", "audio-text-to-text"],
                "model_card_text": "MOSS Transcribe Diarize performs transcription with speaker diarization.",
            },
            "auto",
        )
        self.assertTrue(matched)
        self.assertEqual(task_type, "sa_asr")
        self.assertEqual(match_source, "research_narrowing")
        self.assertGreaterEqual(score, 0.95)
        self.assertIn("task_narrowing.final_task", {item.get("field") for item in evidence})

    def test_auto_prefers_a_tag_task_over_a_description_mention(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "modelscope",
                "model_id": "owner/who-spoke-when",
                "repo": "https://modelscope.cn/models/owner/who-spoke-when",
                "tags": ["speaker-diarization"],
                "description": "Segments a meeting by speaker; pairs with any speech recognition backend.",
            },
            "auto",
        )
        self.assertTrue(matched)
        self.assertEqual(task_type, "sd")
        self.assertEqual(match_source, "tags")
        self.assertGreater(score, 0)

    def test_auto_refuses_to_guess_between_two_tagged_tasks(self) -> None:
        with self.assertRaises(ValueError) as raised:
            infer_task(
                {
                    "source": "github",
                    "model_id": "owner/speech-toolkit",
                    "repo": "https://github.com/owner/speech-toolkit",
                    "tags": ["speech-recognition", "text-to-speech"],
                },
                "auto",
            )
        message = str(raised.exception)
        self.assertIn("--task", message)
        self.assertIn("asr", message)
        self.assertIn("tts", message)

    def test_auto_reports_no_match_rather_than_failing_when_nothing_matches(self) -> None:
        matched, task_type, score, evidence, match_source = infer_task(
            {
                "source": "github",
                "model_id": "owner/generic-toolkit",
                "repo": "https://github.com/owner/generic-toolkit",
                "description": "A generic audio toolkit.",
            },
            "auto",
        )
        self.assertFalse(matched)
        self.assertEqual(task_type, "auto")
        self.assertEqual(score, 0.0)
        self.assertEqual(evidence, [])
        self.assertEqual(match_source, "")

    def test_github_never_defaults_weights_source_to_github(self) -> None:
        model_input, weak_fields, _evidence = synthesize_model_input(
            {
                "source": "github",
                "model_id": "owner/repo",
                "repo": "https://github.com/owner/repo",
            },
            "asr",
        )
        self.assertEqual(model_input["weights"]["source"], "release_or_pypi")
        self.assertIn("missing:weights.source", weak_fields)

    def test_github_weak_weights_fallback_is_marked_weak(self) -> None:
        weights_source, reason = GitHubProvider()._infer_weights_source("# Demo ASR\nRun `python demo.py`.", None)
        _model_input, weak_fields, _evidence = synthesize_model_input(
            {
                "source": "github",
                "model_id": "owner/repo",
                "repo": "https://github.com/owner/repo",
                "github_weights_source": weights_source,
                "github_weights_source_reason": reason,
            },
            "asr",
        )
        self.assertIn("weak:weights.source", weak_fields)

    def test_yaml_emits_empty_lists_as_empty_sequences(self) -> None:
        self.assertIn("system_packages: []", to_yaml({"system_packages": []}))

    def test_yaml_quotes_multiline_strings(self) -> None:
        rendered = to_yaml({"infer_test": "python server.py \\\n--host 0.0.0.0"})
        self.assertIn("\\n", rendered)
        self.assertNotIn("\n--host", rendered)

    def test_yaml_quotes_numeric_strings(self) -> None:
        self.assertIn('python_version: "3.10"', to_yaml({"python_version": "3.10"}))


if __name__ == "__main__":
    unittest.main()
