import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const doctor = resolve(import.meta.dirname, "sure-doctor.mjs");
const suffix = process.platform === "win32" ? ".exe" : "";

// The agent resolves rg and fd with getToolPath() in
// packages/coding-agent/src/utils/tools-manager.ts, which reads its own bin
// directory before PATH. The doctor runs outside the repository root on
// purpose: these cover the tool lookup, not the checkout checks.
function runDoctor(fillManagedBinary) {
	const home = mkdtempSync(join(tmpdir(), "sure-doctor-test-"));
	const binDir = join(home, ".pi", "agent", "bin");
	try {
		mkdirSync(binDir, { recursive: true });
		for (const name of ["rg", "fd"]) fillManagedBinary(join(binDir, `${name}${suffix}`));

		const env = { ...process.env, HOME: home, USERPROFILE: home };
		for (const key of Object.keys(env)) {
			if (/^(path|pi_coding_agent_dir)$/i.test(key)) delete env[key];
		}
		// Nothing executable lives in this directory, so PATH cannot satisfy the checks.
		env.PATH = home;

		const result = spawnSync(process.execPath, [doctor], { cwd: home, encoding: "utf8", env });
		return { binDir, lines: result.stdout.split(/\r?\n/) };
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
}

// A tool installed only in the agent bin directory must not be reported as
// missing. node is the one executable every host running this test has.
test("finds rg and fd in the agent bin directory when they are not on PATH", () => {
	const { binDir, lines } = runDoctor((path) => copyFileSync(process.execPath, path));
	for (const label of ["rg", "fd"]) {
		const line = lines.find((candidate) => candidate.includes(` ${label}: `));
		assert.ok(line?.startsWith("PASS "), `expected a PASS line for ${label}, got: ${line}`);
		assert.ok(line.includes(binDir), `expected ${label} to be reported from ${binDir}, got: ${line}`);
	}
});

// A truncated or non-executable download. getToolPath() hands the agent this
// path on existsSync alone, so PATH is no fallback for it and the doctor must
// not report the host as healthy.
test("warns when the managed rg and fd exist but cannot run", () => {
	const { binDir, lines } = runDoctor((path) => writeFileSync(path, ""));
	for (const label of ["rg", "fd"]) {
		const line = lines.find((candidate) => candidate.includes(` ${label}: `));
		assert.ok(line?.startsWith("WARN "), `expected a WARN line for ${label}, got: ${line}`);
		assert.ok(line.includes(binDir), `expected ${label} to name the managed path, got: ${line}`);
	}
});

// The runtime directory is named after a hash of the lock file, so "is it materialized"
// is a question the doctor can answer without starting a bootstrap: rebuild the id from
// the spec and the current lock, then look for the directory.
function makeRuntimeRepo({ kind, materialized, manifest }) {
	const repo = mkdtempSync(join(tmpdir(), "sure-doctor-runtime-"));
	writeFileSync(join(repo, "package.json"), JSON.stringify({ name: "pi-monorepo" }));
	const specDir = join(repo, "sure", "runtime", kind);
	mkdirSync(specDir, { recursive: true });
	const lockPath = join(specDir, "requirements.lock.txt");
	writeFileSync(lockPath, "example-lock\n");
	const fullLockSha = createHash("sha256").update(readFileSync(lockPath)).digest("hex");
	const lockSha = fullLockSha.slice(0, 12);
	const spec =
		kind === "harness"
			? {
					schema: "sure.harness.runtime.spec.v1",
					harness_version: "v1",
					materialization_version: 4,
					python: "3.11",
					lock_file: "requirements.lock.txt",
				}
			: {
					schema: "sure.evaluation.runtime.spec.v1",
					runtime_version: "root-v1",
					materialization_version: 5,
					python: "3.11",
					engine_commit: "a".repeat(40),
					lock_file: "requirements.lock.txt",
				};
	writeFileSync(join(specDir, "runtime.json"), JSON.stringify(spec));
	const id =
		kind === "harness"
			? `sure-harness-v1-m4-py311-${lockSha}`
			: `sure-evaluation-root-v1-m5-${"a".repeat(12)}-py311-${lockSha}`;
	if (materialized) {
		const runtimeDir = join(repo, "sure", ".runtime", kind, id);
		mkdirSync(join(runtimeDir, "base-python"), { recursive: true });
		const interpreterDir = process.platform === "win32" ? "Scripts" : "bin";
		const interpreterName = process.platform === "win32" ? "python.exe" : "python";
		mkdirSync(join(runtimeDir, interpreterDir), { recursive: true });
		writeFileSync(join(runtimeDir, interpreterDir, interpreterName), "");
		// A manifest that agrees with the spec and the lock, as bootstrap.py writes one.
		const content = manifest ?? {
			schema: "sure.harness.runtime.manifest.v1",
			runtime_id: id,
			lock_sha256: fullLockSha,
			base_python_relative: "base-python",
		};
		writeFileSync(join(runtimeDir, "runtime-manifest.json"), JSON.stringify(content));
	}
	return { repo, id };
}

test("reports a materialized runtime as PASS", () => {
	const { repo, id } = makeRuntimeRepo({ kind: "harness", materialized: true });
	try {
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout.split(/\r?\n/).find((candidate) => candidate.startsWith("PASS harness runtime"));
		assert.ok(line, `expected a PASS for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes(id), `expected the runtime id ${id} in: ${line}`);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the manifest disagrees with the spec or the lock", () => {
	// A directory that exists is not a runtime: resolve.ts rejects a manifest whose id or
	// lock hash does not match and rebuilds, so the doctor must not call it materialized.
	const { repo, id } = makeRuntimeRepo({
		kind: "harness",
		materialized: true,
		manifest: { lock_sha256: "0".repeat(64), base_python_relative: "base-python" },
	});
	try {
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout.split(/\r?\n/).find((candidate) => candidate.startsWith("WARN harness runtime"));
		assert.ok(line, `expected a WARN for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes(id), `expected the runtime id ${id} in: ${line}`);
		assert.ok(line.includes("not usable"), `expected the unusable message in: ${line}`);
		assert.ok(line.includes("lock_sha256"), `expected the lock mismatch in: ${line}`);
		assert.ok(line.includes("runtime_id"), `expected the missing id in: ${line}`);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the manifest points at an interpreter outside the runtime", () => {
	const { repo, id } = makeRuntimeRepo({
		kind: "harness",
		materialized: true,
		manifest: { lock_sha256: "0".repeat(64), base_python_relative: "base-python", python_executable: process.execPath },
	});
	try {
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout.split(/\r?\n/).find((candidate) => candidate.startsWith("WARN harness runtime"));
		assert.ok(line, `expected a WARN for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes("escapes the runtime directory"), line);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the runtime interpreter is missing", () => {
	const { repo, id } = makeRuntimeRepo({ kind: "harness", materialized: true });
	const interpreterDir = process.platform === "win32" ? "Scripts" : "bin";
	const interpreterName = process.platform === "win32" ? "python.exe" : "python";
	try {
		rmSync(join(repo, "sure", ".runtime", "harness", id, interpreterDir, interpreterName));
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout.split(/\r?\n/).find((candidate) => candidate.startsWith("WARN harness runtime"));
		assert.ok(line, `expected a WARN for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes("runtime interpreter is missing"), line);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the runtime interpreter symlink escapes the runtime", { skip: process.platform === "win32" }, () => {
	const { repo, id } = makeRuntimeRepo({ kind: "harness", materialized: true });
	const interpreter = join(repo, "sure", ".runtime", "harness", id, "bin", "python");
	try {
		rmSync(interpreter);
		symlinkSync(process.execPath, interpreter);
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout.split(/\r?\n/).find((candidate) => candidate.startsWith("WARN harness runtime"));
		assert.ok(line, `expected a WARN for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes("runtime interpreter escapes"), line);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the runtime is not materialized and names the prepare command", () => {
	const { repo, id } = makeRuntimeRepo({ kind: "evaluation", materialized: false });
	try {
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout
			.split(/\r?\n/)
			.find((candidate) => candidate.startsWith("WARN evaluation runtime"));
		assert.ok(line, `expected a WARN for the evaluation runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes(id), `expected the runtime id ${id} in: ${line}`);
		assert.ok(line.includes("--prepare"), `expected the prepare command in: ${line}`);
		// The command cannot run on its own: it needs a bound Harness Runtime first, and
		// a user who copies it out of the WARN must be told so.
		assert.ok(line.includes("bound Harness Runtime"), `expected the binding note in: ${line}`);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("an evaluation spec without version fields falls back to the generator defaults", () => {
	const { repo } = makeRuntimeRepo({ kind: "evaluation", materialized: false });
	try {
		const specPath = join(repo, "sure", "runtime", "evaluation", "runtime.json");
		const spec = JSON.parse(readFileSync(specPath, "utf8"));
		delete spec.runtime_version;
		delete spec.materialization_version;
		writeFileSync(specPath, JSON.stringify(spec));
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout
			.split(/\r?\n/)
			.find((candidate) => candidate.startsWith("WARN evaluation runtime"));
		assert.ok(line, `expected a WARN for the evaluation runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes("sure-evaluation-root-v1-m1-"), `expected the default id in: ${line}`);
		assert.ok(!line.includes("undefined"), `expected no undefined in: ${line}`);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("a harness spec without version fields is refused instead of given a wrong id", () => {
	const { repo } = makeRuntimeRepo({ kind: "harness", materialized: false });
	try {
		const specPath = join(repo, "sure", "runtime", "harness", "runtime.json");
		const spec = JSON.parse(readFileSync(specPath, "utf8"));
		delete spec.harness_version;
		delete spec.materialization_version;
		writeFileSync(specPath, JSON.stringify(spec));
		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout
			.split(/\r?\n/)
			.find((candidate) => candidate.startsWith("WARN harness runtime"));
		assert.ok(line, `expected a WARN for the harness runtime, stdout:\n${result.stdout}`);
		assert.ok(line.includes("must declare"), `expected the missing-fields message in: ${line}`);
		assert.ok(!line.includes("undefined"), `expected no undefined in: ${line}`);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

// docs/evaluation_engine.md: /sure_eval refuses a route plan when the engine
// checkout and runtime.json disagree. The gitlink and the checkout can be
// updated together while runtime.json is left behind, so comparing only those
// two reported PASS for a host that cannot evaluate at all.
test("warns when the submodule matches the gitlink but not the runtime lock", () => {
	const repo = mkdtempSync(join(tmpdir(), "sure-doctor-engine-"));
	try {
		writeFileSync(join(repo, "package.json"), JSON.stringify({ name: "pi-monorepo" }));

		const engine = join(repo, "sure", "external", "sure-evaluation");
		mkdirSync(engine, { recursive: true });
		writeFileSync(join(engine, "pyproject.toml"), '[project]\nname = "engine"\n');
		execFileSync("git", ["init", "-q"], { cwd: engine });
		execFileSync("git", ["add", "."], { cwd: engine });
		execFileSync(
			"git",
			["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "engine"],
			{ cwd: engine },
		);
		const head = execFileSync("git", ["rev-parse", "HEAD"], { cwd: engine, encoding: "utf8" }).trim();

		execFileSync("git", ["init", "-q"], { cwd: repo });
		writeFileSync(
			join(repo, ".gitmodules"),
			'[submodule "sure/external/sure-evaluation"]\n\tpath = sure/external/sure-evaluation\n\turl = https://example.invalid/engine.git\n',
		);
		execFileSync(
			"git",
			["update-index", "--add", "--cacheinfo", `160000,${head},sure/external/sure-evaluation`],
			{ cwd: repo },
		);
		mkdirSync(join(repo, "sure", "runtime", "evaluation"), { recursive: true });
		writeFileSync(
			join(repo, "sure", "runtime", "evaluation", "runtime.json"),
			JSON.stringify({ engine_commit: "0".repeat(40) }),
		);

		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout
			.split(/\r?\n/)
			.find((candidate) => candidate.startsWith("WARN sure-evaluation submodule"));
		assert.ok(line, `expected a WARN for the runtime-lock drift, stdout:\n${result.stdout}`);
		assert.ok(line.includes("runtime.json locks"), line);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});

test("warns when the runtime lock has no valid engine commit", () => {
	const repo = mkdtempSync(join(tmpdir(), "sure-doctor-engine-lock-"));
	try {
		writeFileSync(join(repo, "package.json"), JSON.stringify({ name: "pi-monorepo" }));
		const engine = join(repo, "sure", "external", "sure-evaluation");
		mkdirSync(engine, { recursive: true });
		writeFileSync(join(engine, "pyproject.toml"), '[project]\nname = "engine"\n');
		execFileSync("git", ["init", "-q"], { cwd: engine });
		execFileSync("git", ["add", "."], { cwd: engine });
		execFileSync(
			"git",
			["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "engine"],
			{ cwd: engine },
		);
		const head = execFileSync("git", ["rev-parse", "HEAD"], { cwd: engine, encoding: "utf8" }).trim();

		execFileSync("git", ["init", "-q"], { cwd: repo });
		writeFileSync(
			join(repo, ".gitmodules"),
			'[submodule "sure/external/sure-evaluation"]\n\tpath = sure/external/sure-evaluation\n\turl = https://example.invalid/engine.git\n',
		);
		execFileSync(
			"git",
			["update-index", "--add", "--cacheinfo", `160000,${head},sure/external/sure-evaluation`],
			{ cwd: repo },
		);
		mkdirSync(join(repo, "sure", "runtime", "evaluation"), { recursive: true });
		writeFileSync(join(repo, "sure", "runtime", "evaluation", "runtime.json"), JSON.stringify({}));

		const result = spawnSync(process.execPath, [doctor], { cwd: repo, encoding: "utf8" });
		const line = result.stdout
			.split(/\r?\n/)
			.find((candidate) => candidate.startsWith("WARN sure-evaluation submodule"));
		assert.ok(line, `expected a WARN for the invalid runtime lock, stdout:\n${result.stdout}`);
		assert.ok(line.includes("no valid engine_commit"), line);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});
