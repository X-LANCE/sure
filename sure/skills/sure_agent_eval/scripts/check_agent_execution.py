#!/usr/bin/env python3
"""Gate script for the run_agent unit: validate execution_result.json.

Read-only. Called by the Sure hook with:
    python3 scripts/check_agent_execution.py --run-dir <runDir> --produces <abs>

A terminal failure is a valid outcome of this gate: the run report then has to
say so. What the gate refuses is an inconsistent record — a job status outside
the contract, a product directory that differs from the resolved plan, or a
"succeeded" run whose bundle does not back the claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _prediction_keys(path: Path) -> list[str]:
    """One key per row that carries an answer: `key<TAB>text`. Duplicates stay."""
    keys: list[str] = []
    if not path.is_file():
        return keys
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t", 1)
        if len(parts) > 1 and parts[1].strip():
            keys.append(parts[0])
    return keys


def _nonempty_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if line.strip())


def _reference_keys(path: Path) -> tuple[set[str], list[int]]:
    """The keys a reference projection expects, plus the 1-based lines that are not valid JSON.

    A row without a usable key is skipped. A row that does not parse is not: skipping it
    dropped its key from the reconciliation and reopened the gap this check exists to close.
    """
    keys: set[str] = set()
    bad_lines: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad_lines.append(number)
            continue
        if isinstance(row, dict):
            # Same key derivation as agent_runner.py, so both sides agree on what a key is.
            key = str(row.get("key") or "")
            if key:
                keys.add(key)
    return keys, bad_lines


# Same rule as agent_runner._stage_output_modality, repeated here so the gate keeps its
# stdlib-only import surface. The runner records output_modality in the resolved spec; an
# older plan without it falls back to mode plus task.
AUDIO_OUTPUT_TASKS = {"SE", "SPEECH_ENHANCEMENT"}


def _stage_output_modality(stage: dict[str, Any]) -> str:
    recorded = str(stage.get("output_modality") or "").strip().lower()
    if recorded in ("audio", "text"):
        return recorded
    task = str(stage.get("task") or "").strip().upper().replace("-", "_").replace(" ", "_")
    return "audio" if stage.get("mode") == "mcp_tool" and task in AUDIO_OUTPUT_TASKS else "text"


def _enhanced_evidence_errors(
    product_dir: Path,
    stages: list[Any],
    dataset: str,
    expected: int,
    expected_keys: set[str],
) -> list[str]:
    """Evidence for every speech-enhancement stage: present, complete, still on disk.

    agent_runner writes one `key<TAB>path` row per sample it chained forward. Nothing used to
    read these files: a stage could produce no evidence, half of it, or point at audio that a
    later run deleted, and the gate still reported a clean run.
    """
    errors: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict) or _stage_output_modality(stage) != "audio":
            continue
        stage_id = str(stage.get("id") or "")
        evidence = product_dir / "enhanced" / stage_id / f"{dataset}.tsv"
        relative = evidence.relative_to(product_dir).as_posix()
        if not evidence.is_file():
            errors.append(f"{dataset}: speech-enhancement stage {stage_id!r} has no evidence file {relative}")
            continue
        rows = 0
        keys: set[str] = set()
        for number, line in enumerate(evidence.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) not in (2, 3):
                errors.append(f"{dataset}: {relative}:{number} must be key<TAB>absolute audio path[<TAB>sha256]")
                continue
            rows += 1
            key, audio = parts[0], parts[1]
            keys.add(key)
            resolved = Path(audio)
            if not resolved.is_absolute() or not resolved.is_file():
                errors.append(f"{dataset}: {relative}:{number} points at a missing or relative audio path: {audio}")
                continue
            # A row written before the digest column existed carries two fields and is
            # accepted on the path alone: that is all it can support, and rejecting it would
            # break every bundle produced by an earlier release. New rows carry the digest,
            # so the file the row names is provably the file that stage produced.
            if len(parts) == 3 and parts[2] != _sha256(resolved):
                errors.append(f"{dataset}: {relative}:{number} sha256 does not match the audio it names")
        if rows != expected:
            errors.append(f"{dataset}: {relative} carries {rows} row(s), expected {expected}")
        if len(keys) != rows:
            errors.append(f"{dataset}: {relative} repeats {rows - len(keys)} key(s)")
        # Shape alone is not enough: a file with the right number of rows can carry
        # duplicated keys, or keys that belong to another dataset. Compare against the
        # reference the run was planned from, the same way the prediction file is checked.
        if expected_keys:
            missing = sorted(expected_keys - keys)
            if missing:
                listed = ", ".join(missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
                errors.append(f"{dataset}: {relative} has no evidence for {len(missing)} reference key(s): {listed}")
            extra = sorted(keys - expected_keys)
            if extra:
                listed = ", ".join(extra[:3]) + (f" (+{len(extra) - 3} more)" if len(extra) > 3 else "")
                errors.append(f"{dataset}: {relative} carries {len(extra)} key(s) the reference does not name: {listed}")
    return errors


def gate_errors(run_dir: Path, result_path: Path) -> list[str]:
    result = _read_json(result_path)
    if result is None:
        return [f"execution_result.json not found or invalid: {result_path}"]
    if result.get("schema") != "sure.agent_eval.execution_result.v1":
        return [f"execution_result.json schema must be sure.agent_eval.execution_result.v1, got {result.get('schema')!r}"]
    artifacts = run_dir / "artifacts"
    spec = _read_json(artifacts / "agent_spec_resolved.json")
    if spec is None:
        return ["agent_spec_resolved.json not found or invalid; run scripts/resolve_agent.py first"]

    errors: list[str] = []
    job_status = result.get("job_status")
    if job_status not in ("succeeded", "failed"):
        errors.append(f"job_status must be succeeded|failed, got {job_status!r}")
    result_agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    if str(result_agent.get("name") or "") != str(spec["agent"]["name"]):
        errors.append(
            f"execution_result agent {result_agent.get('name')!r} does not match the resolved spec agent "
            f"{spec['agent']['name']!r}"
        )
    product_dir = Path(str(result.get("product_dir") or ""))
    if str(product_dir) != str(spec["runtime"]["product_dir"]):
        errors.append(
            f"product_dir {product_dir} differs from the resolved plan {spec['runtime']['product_dir']}"
        )

    if job_status == "failed":
        # str() on anything non-empty passes, so an object-valued field would read
        # as a recorded reason; hooks/validate.ts does not reject it either.
        for field in ("error", "failed_stage"):
            value = result.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"a failed execution_result.json must record {field} as a non-empty string")
        return errors

    rows = result.get("datasets")
    if not isinstance(rows, list) or not rows:
        errors.append("a succeeded execution_result.json must list its datasets")
        return errors
    expected_datasets = [str(item["dataset"]) for item in spec["datasets"]]
    seen = [str(row.get("dataset") or "") for row in rows if isinstance(row, dict)]
    if sorted(seen) != sorted(expected_datasets):
        errors.append(f"execution_result datasets {sorted(seen)} do not match the plan {sorted(expected_datasets)}")
    manifest = _read_json(product_dir / "predictions" / "manifest.json") or {}
    manifest_rows = manifest.get("datasets") if isinstance(manifest.get("datasets"), dict) else {}
    status = _read_json(product_dir / "prediction_generation_status.json") or {}
    status_rows = {
        str(item.get("dataset") or ""): str(item.get("status") or "")
        for item in status.get("datasets") or []
        if isinstance(item, dict)
    }
    for row in rows:
        if not isinstance(row, dict):
            errors.append("dataset rows must be objects")
            continue
        dataset = str(row.get("dataset") or "")
        expected = row.get("expected")
        generated = row.get("generated")
        # Defaulting these to 0 made every check below compare 0 against 0, so a
        # row that declared no counts was a row nothing could fail.
        if not all(isinstance(count, int) and not isinstance(count, bool) for count in (expected, generated)):
            errors.append(f"{dataset}: dataset rows must declare expected and generated as integers")
            continue
        prediction_file = product_dir / "predictions" / f"{dataset}.txt"
        row_keys = _prediction_keys(prediction_file)
        non_empty = len(row_keys)
        if generated != expected:
            errors.append(f"{dataset}: generated {generated} != expected {expected}")
        if non_empty != expected:
            errors.append(f"{dataset}: predictions/{dataset}.txt carries {non_empty} non-empty rows, expected {expected}")
        recorded = manifest_rows.get(dataset) if isinstance(manifest_rows.get(dataset), dict) else {}
        if prediction_file.is_file() and str(recorded.get("sha256") or "") != _sha256(prediction_file):
            errors.append(
                f"{dataset}: predictions/manifest.json sha256 {recorded.get('sha256')!r} does not match "
                f"predictions/{dataset}.txt"
            )
        if status_rows.get(dataset) != "completed":
            errors.append(f"{dataset}: prediction_generation_status.json status is {status_rows.get(dataset)!r}")
        reference = product_dir / "references" / "sure_benchmark" / "jsonl" / f"{dataset}.jsonl"
        evidence_reference_keys: set[str] = set()
        if not reference.is_file():
            errors.append(f"{dataset}: missing reference projection {reference}")
        elif _nonempty_lines(reference) < expected:
            errors.append(f"{dataset}: reference projection carries fewer than {expected} rows")
        else:
            # A row count can match while the keys do not: the file can carry the right
            # number of answers under keys the reference never names. /sure_eval reports
            # that only after it has started, so the cheapest place to catch it is here.
            reference_keys, bad_lines = _reference_keys(reference)
            for number in bad_lines:
                errors.append(f"{dataset}: reference projection line {number} is not valid JSON")
            if not bad_lines:
                evidence_reference_keys = reference_keys
            predicted = set(row_keys)
            missing = sorted(reference_keys - predicted)
            if missing:
                listed = ", ".join(missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
                errors.append(
                    f"{dataset}: predictions/{dataset}.txt has no row for {len(missing)} reference key(s): {listed}"
                )
        # At loop level, not inside the else above: a run whose reference projection is
        # short still owes evidence for its speech-enhancement stages.
        errors.extend(
            _enhanced_evidence_errors(
                product_dir,
                spec.get("stages") or [],
                dataset,
                expected,
                evidence_reference_keys,
            )
        )
    if not (product_dir / "protocol.yaml").is_file():
        errors.append(f"protocol.yaml missing from product directory {product_dir}")
    if not (product_dir / "predictions" / "manifest.json").is_file():
        errors.append(f"predictions/manifest.json missing from product directory {product_dir}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate execution_result.json for /sure_agent_eval")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--produces", required=True, help="absolute path to execution_result.json")
    args = parser.parse_args()
    errors = gate_errors(Path(args.run_dir), Path(args.produces))
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
