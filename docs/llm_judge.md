# SLU LLM Judge

The optional `llm_judge` metric evaluates textual SLU answers using a configured
OpenAI-compatible service. It does not process audio.

## Configuration

Save the following JSON as `judge.json` in a local configuration directory outside
the repository, invocation scratch directory, and prediction bundle, for example
`/home/you/.config/sure/judge.json`. Pass its absolute path as `llm_config` to
`/sure_eval`. Relative configuration paths resolve from the SURE repository root.

```json
{
  "base_url": "https://your-provider.example/v1",
  "model": "your-judge-model-id",
  "rubric": "Evaluate the answer using only the supplied question, context, choices and optional reference answer. Award 100 for a fully correct answer, 50 for a partially correct answer, and 0 for an incorrect answer when the evidence is sufficient. Use intermediate scores for intermediate quality. If the evidence is insufficient to judge, return score=null and explain what evidence is missing.",
  "api_key_env": "LLM_JUDGE_API_KEY",
  "temperature": 0,
  "max_tokens": 1024,
  "timeout": 60
}
```

Replace the example base_url and model ID with values supported by your provider.

| Field | Meaning |
| --- | --- |
| `base_url` | API base URL, including the provider's version prefix when required. The adapter appends `/chat/completions`; do not include that suffix here. |
| `model` | Exact Judge model identifier accepted by the configured service. |
| `rubric` | Required nonempty, user-authored scoring criteria for the 0–100 range. Provide enough textual evidence to apply them. Insufficient evidence produces a null sample score. API failures and invalid responses also mark samples as failed; any sample failure makes the batch score `null`. |
| `api_key_env` | Name of the environment variable containing the API key, not the key itself. Set it in the environment that launches SURE. |
| `temperature` | Finite non-negative sampling temperature accepted by the provider. |
| `max_tokens` | Positive integer limiting the generated response tokens. |
| `timeout` | Positive finite timeout in seconds passed to the HTTP request; defaults to 60. This is not an overall batch deadline. |

Non-local endpoints require **HTTPS**. Plain HTTP is permitted only for
`localhost`, `127.0.0.1`, and `::1`. The example base URL produces the request URL
`https://your-provider.example/v1/chat/completions`. The service must support
OpenAI-compatible Chat Completions.

Set the key before launching SURE, for example in Bash on Linux or WSL:

```bash
export LLM_JUDGE_API_KEY="<your-api-key>"
```


## Inputs

`/sure_eval` reads the dataset JSONL and existing predictions from the selected
`/sure_infer` bundle.  Dataset rows supply the
question and textual evidence. `predictions/<dataset>.txt` supplies the candidate
answer as `key<TAB>text`.

Minimal dataset row in the bundle's `references/sure_benchmark/jsonl/` tree:

```jsonl
{"key":"q1","task":"SLU","language":"en","prompt":"Which city does the speaker plan to visit?","context":"Transcript: We will visit Paris.","choices":{"A":"Paris","B":"London"},"target":"Paris"}
```

Matching prediction row:

```text
q1	Paris
```



| Input | Contract and value passed to the Judge |
| --- | --- |
| `key` | Required nonempty, unique string identifying the sample. Prompt, prediction, and supplied reference files must have the same nonempty key set. |
| `prompt` / `question` | Required nonempty question string. A truthy `prompt` takes precedence over `question`; passed as `question`. |
| `context` | Optional string containing textual evidence, such as an audio transcript. Passed as `context`; an audio path does not provide audio understanding. |
| `choices` / `options` | Optional list or object. `choices` takes precedence when that field exists; passed as `choices`. |
| Candidate answer | Original text from the prediction file, passed as `answer`. It is not converted to an option label or normalized for accuracy. An empty answer is allowed for the rubric to assess. |
| Reference answer | Optional aligned reference text, passed as `reference_answer`. In this bundle example the harness extracts it from `target`; the engine receives a separate `ref_file`, not a reference inferred from arbitrary prompt metadata. |

For the sample above, the Judge's user message contains:

```json
{
  "question": "Which city does the speaker plan to visit?",
  "context": "Transcript: We will visit Paris.",
  "choices": {"A": "Paris", "B": "London"},
  "answer": "Paris",
  "reference_answer": "Paris"
}
```

The harness materializes `prompts.jsonl` from the matching dataset rows, a
`key<TAB>answer` hypothesis file, and an optional `key<TAB>reference` file for
the engine. If all references are absent, the Judge route omits the reference
file. If any reference is supplied, every sample must have a nonempty aligned
reference; partial reference coverage is an error. 

The harness uses the first truthy reference value in this order: `target`,
`target_text`, `ground_truth`, `label`, `answer`, `intent`, `reference_text`,
`text`. A dataset's `answer` field therefore denotes a reference; the candidate
answer always comes from the prediction file.

To judge without references, omit `target` and other reference fields from the
dataset rows and provide sufficient evidence in the question/context. The Judge
cannot hear the original audio. When the supplied text cannot establish an
audio fact, it must return a null score with an explanation of the missing
evidence; that sample is recorded as `insufficient_evidence`, not scored zero.

## Execution

Run `/sure_init` first. 

The command below uses these example values:

| Parameter | Example value | Meaning |
| --- | --- | --- |
| `model` | `ExampleSLU` | Approved model directory name. |
| `datasets` | `example_questions__v1__slu` | Complete dataset set in the existing bundle. |
| `source` | `/data/sure/results/ExampleSLU/standard_system/sure_infer_20261112_100000` | Existing, completed `/sure_infer` bundle directory. |
| `protocol` | `standard_system` | Protocol used by the source bundle. |

Replace these illustrative values with the actual model, complete dataset set,
bundle path, and protocol from your completed run.

1. Save the configuration above as `/home/you/.config/sure/judge.json`, replacing
   the service URL and model ID. Keep it outside the repository and result bundle.
2. Set `LLM_JUDGE_API_KEY` in the shell that launches SURE. Agent authentication
   from `/sure_init` does not supply the Judge service's API key automatically.
3. Enter this command in the SURE agent session (not directly in Bash):

```text
/sure_eval model=ExampleSLU datasets=example_questions__v1__slu source=/data/sure/results/ExampleSLU/standard_system/sure_infer_20261112_100000 protocol=standard_system metrics=llm_judge llm_config=/home/you/.config/sure/judge.json
```

`metrics=llm_judge` resolves to `slu.any.llm_judge.llm_judge_v1`. To select it
explicitly, replace `metrics=llm_judge` with
`pipeline_id=slu.any.llm_judge.llm_judge_v1`.

`env check` validates runtime
availability; it does not test credentials, connectivity or scoring quality.
The node processes samples sequentially with one request per attempted sample
and no automatic retry. HTTP redirects are refused rather than forwarding
Authorization to a new location. Requests send the instruction/rubric and sample
question, context, choices, candidate answer and optional reference to the chosen
provider.

All-success batches use the ordinary SURE dataset/metric report schema. Scores
remain on the native 0–100 scale: `80` is displayed as `80.00/100`. Integers and finite decimals are accepted;
booleans, strings, non-finite values and values outside 0–100 are rejected.

Judge RPS is currently unavailable (`rps=null`).

## Failures

Each successful sample has a numeric `score` and nonempty `reason`. The node's
handled sample failures are listed below. They are recorded as `status="failed"`
with an `error_code`; later samples continue without automatic retries.

| Error code | Trigger |
| --- | --- |
| `insufficient_evidence` | The Judge returns `score=null` with a nonempty reason explaining missing evidence. This failure record preserves `score=null` and `reason`. |
| `http_error_<status>` | The service returns an HTTP error, such as 401, 403, 429 or 500. The suffix is the actual HTTP status; refused redirects can also surface as HTTP errors. |
| `api_http_protocol_error` | An HTTP protocol/read exception, including a truncated response body or malformed status line. |
| `api_transport_or_json_error` | A network/socket/TLS error, request timeout, invalid response encoding, or invalid outer response JSON. |
| `incomplete_or_refused_response` | `finish_reason` is not `stop`, or the response declares a refusal. |
| `invalid_response_content` | The response message's `content` is not a string. |
| `invalid_api_response` | Required API response structure is missing or malformed, `choices` is empty, or the message content is not parseable judgment JSON. |
| `invalid_judgment_fields` | The judgment is not an object, or its fields do not match exactly `score` and `reason` after the adapter's response-model metadata is removed. |
| `invalid_judgment_score` | The score is a boolean, string, non-finite number, other unsupported type, or outside 0–100. A null score is handled as insufficient evidence when its reason is valid. |
| `invalid_judgment_reason` | The reason is not a string or is empty/whitespace, including when the score is null. |
| `missing_api_key` | Neither the local `api_key` nor the named environment variable supplies a nonblank key. The request adapter checks this before sending each request; the sample fails without an API call. The standalone node CLI checks credentials before entering the sample loop. |

For example, an insufficient-evidence response is:

```json
{"score": null, "reason": "The supplied text does not establish the audio fact."}
```

Other failure records need not contain a `score` or `reason`.
If any sample fails, `details.result.score` is `null`, `num_failed` counts the
failures, and `num_scored` counts successes. Successful sample judgments remain
available.

Where to inspect failure artifacts:

| Artifact | Location |
| --- | --- |
| Invocation failure report | `.sure/runs/<invocation-id>/artifacts/eval_run_report.json` |
| Raw scoring report, if generated; inspect `pipeline_trace` for sample outcomes | `.sure/runs/<invocation-id>/scratch/evaluation_runs/<dataset>/<metric-or-pipeline-id>/report.json` |

`timeout` in `judge.json` configures the HTTP request timeout (default 60 seconds).
The harness also limits each external evaluator subprocess to 600 seconds by
default, configurable through `SURE_EVALUATION_TIMEOUT` in the launching
environment. For large sequential batches, allow sufficient overall time. A
subprocess timeout can interrupt the batch before its report is written.

Configuration, input and process failures are separate from the per-sample
error codes above. They can stop the run before a complete batch report exists:

| Failure | Trigger and behavior |
| --- | --- |
| Missing configuration | `llm_config` is omitted for a Judge route, or the configuration file does not exist or cannot be read. Evaluation stops. |
| Invalid configuration | Malformed/non-object JSON; missing required fields or unknown fields; empty rubric/model/key-variable name; invalid URL; invalid timeout, temperature, `max_tokens`, optional instruction or API-key type. Validation stops before API requests. |
| Invalid prompt JSONL | Invalid JSON or non-object rows; missing, empty or duplicate string keys; missing/non-string/blank questions; non-string context; or non-list/object choices. Validation stops before API requests. |
| Invalid prediction/reference file | Unreadable files; rows without `key<TAB>text`; empty or duplicate keys; no samples; empty supplied references; or mismatched prompt/prediction/reference key sets. Empty candidate answers are permitted. Validation stops before API requests. |
| Invalid source or route | The harness cannot validate the existing bundle/model/protocol/dataset scope, or the requested route is unsupported. This is a harness/route error, not a Judge sample score. |
| Evaluator process timeout | The external subprocess exceeds its time limit and is interrupted. A complete report is not guaranteed. |
| Artifact/runtime failure | Runtime preparation, report writing, artifact validation or persistence fails. The invocation fails; consult its terminal report and available scratch evidence. |

These categories do not invent new error codes: configuration and input errors
use the existing validation exceptions and harness failure reporting. Correct
the cause before rerunning; a null result is not a numeric zero. Unexpected
implementation exceptions are not converted into sample failures by a broad
catch-all handler.

## Artifacts

On success, the evaluation is appended to the existing bundle:

```text
<bundle>/
├── report.jsonl
├── report_snapshot.md
└── evaluation_runs/<batch-id>/
    ├── artifact_manifest.json
    ├── metrics/<dataset>/llm_judge/
    │   ├── report.json
    │   └── pipeline_description.json
    └── sample_reports/<dataset>/llm_judge.jsonl
```

| Artifact | Key fields or purpose |
| --- | --- |
| `report.jsonl` | Standard report rows: `metric.name=llm_judge`, `metric.score`, `metric.score_range=[0,100]`, `metric.raw_score`, `metric.score_conversion="none"`, `rps=null`, and provenance under `pipeline.judge`. |
| `report_snapshot.md` | Human-readable aggregate report. |
| `metrics/<dataset>/llm_judge/report.json` | Engine score and `pipeline_trace`. Find `node_id="scoring/llm_judge"`; its `details` contains `sample_results`, rubric/configuration provenance and the batch `result`. |
| `metrics/<dataset>/llm_judge/pipeline_description.json` | Resolved pipeline and node identities. |
| `sample_reports/<dataset>/llm_judge.jsonl` | Per-sample prediction, score, reason and Judge status. |
| `artifact_manifest.json` | Persisted batch file inventory and hashes. |

Paths in the last four rows are relative to `evaluation_runs/<batch-id>/`.
Use the actual paths in each report row's `artifacts` field, resolved relative to
the bundle root. Metric filenames can include a pipeline suffix when several
routes share a metric. Successful local bundle evaluations append in place;
approved-result sources append to their local mirror. Existing inference
predictions are not overwritten.

## Reproducibility and provenance

For comparisons, pin and retain all of the following:

- The engine commit in `sure/runtime/evaluation/runtime.json`, the matching
  `sure/external/sure-evaluation` checkout, and runtime locks. Do not substitute
  an arbitrary evaluator checkout.
- The Judge provider/base URL and model ID; prefer an immutable provider model
  version where available. The trace also records returned model identities.
- The exact rubric, optional `instruction`, temperature, `max_tokens` and other
  effective configuration.
- The exact input bundle, predictions, optional references, context and evidence
  policy. Source identity and artifact hashes help track these inputs.

The node trace records the actual rubric, `rubric_sha256`, safe `llm_config`,
`score_range`, precision policy and response model identity. The harness exposes
a safe summary under `pipeline.judge`, including rubric/configuration, hashes,
response models, raw score/range and `score_conversion="none"`.

`rubric_sha256` hashes the UTF-8 rubric text. **`prompt_sha256` hashes only the
combined system instruction**: optional configured instruction, the built-in
answer-evaluation prompt, and user rubric. It does **not** hash per-sample user
messages containing questions, context, choices, candidate answers or references.
Use source/batch evidence as well; equal prompt hashes alone do not mean equal
evaluation inputs. Rubric content hashes are separate from node version `v1`.

An external LLM with temperature zero still does not guarantee bit-for-bit
identical scores. Provider updates, serving implementations and model behavior
can vary. Judge scores are model judgments.

## API-key handling and scope

Use `api_key_env` to keep the actual key in the launching process's environment.
Never commit it or place it in result bundles. The implementation also accepts
an optional local `api_key` value, which takes precedence over the environment;
if used, keep that configuration private and outside tracked files. The actual
key is excluded from node reports, and the harness summary excludes both
`api_key` and `api_key_env`. The configuration file is read in place, not copied
into evaluation artifacts. Rubrics, sample text and nonsecret service/model
configuration do appear in evidence.

Judge is limited to **textual SLU answer quality**. It cannot hear audio or verify
audio facts without sufficient supplied text/reference evidence. It is an
optional route, not a replacement for existing SLU accuracy or its RPS baselines.
