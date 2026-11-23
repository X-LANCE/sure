#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";

const root = process.cwd();
const checks = [];

function record(level, name, detail) {
	checks.push({ level, name, detail });
}

function pass(name, detail) {
	record("pass", name, detail);
}

function warn(name, detail) {
	record("warn", name, detail);
}

function fail(name, detail) {
	record("fail", name, detail);
}

function readJson(path) {
	try {
		return JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return undefined;
	}
}

function isFile(path) {
	try {
		return statSync(path).isFile();
	} catch {
		return false;
	}
}

function isDirectory(path) {
	try {
		return statSync(path).isDirectory();
	} catch {
		return false;
	}
}

function isInside(parent, candidate) {
	const child = relative(parent, candidate);
	return child === "" || (child !== ".." && !child.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) && !isAbsolute(child));
}

function isRealPathInside(parent, candidate) {
	try {
		return isInside(realpathSync(parent), realpathSync(candidate));
	} catch {
		return false;
	}
}

function runGit(args, cwd = root) {
	try {
		return execFileSync("git", args, {
			cwd,
			encoding: "utf8",
			stdio: ["ignore", "pipe", "pipe"],
		}).trim();
	} catch {
		return undefined;
	}
}

// No `which` / `where`: asking the binary for its own version is the one probe that
// behaves the same on Linux, macOS and Windows. Same shape as commandExists() in
// packages/coding-agent/src/utils/tools-manager.ts, so no shell either; a Windows
// tool installed only as a .cmd shim reads as missing, which is a warning here.
function commandVersion(command) {
	try {
		return execFileSync(command, ["--version"], {
			encoding: "utf8",
			stdio: ["ignore", "pipe", "pipe"],
			timeout: 15_000,
		})
			.split(/\r?\n/)[0]
			.trim();
	} catch {
		return undefined;
	}
}

function versionAtLeast(actual, expected) {
	const a = actual.split(".").map((part) => Number.parseInt(part, 10));
	const e = expected.split(".").map((part) => Number.parseInt(part, 10));
	for (let i = 0; i < Math.max(a.length, e.length); i += 1) {
		const av = a[i] ?? 0;
		const ev = e[i] ?? 0;
		if (av > ev) return true;
		if (av < ev) return false;
	}
	return true;
}

const pkgPath = join(root, "package.json");
const pkg = readJson(pkgPath);
if (!pkg || pkg.name !== "pi-monorepo") {
	fail("repository root", `run this from the SURE Harness repository root; current cwd is ${root}`);
} else {
	pass("repository root", root);
}

if (versionAtLeast(process.versions.node, "22.19.0")) {
	pass("node version", process.versions.node);
} else {
	fail("node version", `found ${process.versions.node}, need >=22.19.0`);
}

const requiredPaths = [
	"pi-test.sh",
	"node_modules/.bin/tsx",
	"packages/coding-agent/src/cli.ts",
	"packages/coding-agent/src/core/sure/module-loader.ts",
	"sure/skills/sure_feed/sure.skill.json",
	"sure/skills/sure_approve/sure.skill.json",
	"sure/skills/sure_onboard/sure.skill.json",
	"sure/skills/sure_trans/sure.skill.json",
	"sure/skills/sure_infer/sure.skill.json",
	"sure/skills/sure_eval/sure.skill.json",
	"sure/skills/sure_agent_eval/sure.skill.json",
	"fixtures",
];

for (const relPath of requiredPaths) {
	const absPath = join(root, relPath);
	if (existsSync(absPath)) {
		pass(relPath, "found");
	} else {
		fail(relPath, "missing; run npm install --ignore-scripts and make sure the checkout is complete");
	}
}

try {
	const moduleLoaderPath = resolve(root, "packages/coding-agent/src/core/sure/module-loader.ts");
	const req = createRequire(moduleLoaderPath);
	for (const specifier of ["typebox", "typebox/compile", "typebox/value"]) {
		pass(`resolve ${specifier}`, req.resolve(specifier));
	}
} catch (error) {
	fail(
		"TypeBox resolution",
		`${error instanceof Error ? error.message : String(error)}; run npm install --ignore-scripts from the repository root`,
	);
}

const sparseCheckoutPath = join(root, ".git", "info", "sparse-checkout");
if (existsSync(sparseCheckoutPath)) {
	const sparseCheckout = readFileSync(sparseCheckoutPath, "utf8");
	for (const relPath of ["scripts", "fixtures"]) {
		if (existsSync(join(root, relPath))) {
			pass(`sparse path ${relPath}`, "present");
		} else {
			warn(`sparse path ${relPath}`, `not present; add it with git sparse-checkout add ${relPath}`);
		}
	}
	if (!sparseCheckout.trim()) {
		warn("sparse checkout", "sparse-checkout file exists but is empty");
	}
}

const uvVersion = commandVersion("uv");
if (uvVersion) {
	pass("uv", uvVersion);
} else {
	warn(
		"uv",
		'not found on PATH; the harness, evaluation and model runtimes are materialized with uv. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh (Linux/macOS) or powershell -c "irm https://astral.sh/uv/install.ps1 | iex" (Windows)',
	);
}

const gitVersion = commandVersion("git");
if (gitVersion) {
	pass("git", gitVersion);
} else {
	warn("git", "not found on PATH; cloning, submodules and the public export all need it");
}

// Mirrors the resolution order in packages/coding-agent/src/config.ts getAgentDir():
// PI_CODING_AGENT_DIR env override, else os.homedir()/.pi/agent.
const agentDir = process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");

// Command names and fallback wording follow TOOLS in
// packages/coding-agent/src/utils/tools-manager.ts, including Debian's fdfind.
// Same lookup order as getToolPath() there: the managed binary in getBinDir()
// (getAgentDir()/bin) first, only then the system names on PATH.
const binDir = join(agentDir, "bin");
for (const [label, commands, fallbackNote] of [
	["rg", ["rg"], "the grep tool falls back to a slower built-in search"],
	["fd", ["fd", "fdfind"], "file autocomplete stops offering files and the find tool falls back to a slower built-in scan"],
]) {
	let found;
	const managedPath = join(binDir, commands[0] + (process.platform === "win32" ? ".exe" : ""));
	if (existsSync(managedPath)) {
		// getToolPath() hands the agent this path on existsSync alone, so a
		// truncated or non-executable download is what it will run and PATH is
		// no fallback for it: report the host as broken, not as healthy.
		const managedVersion = commandVersion(managedPath);
		if (!managedVersion) {
			warn(label, `${managedPath} exists but cannot be run; the agent uses it before PATH, so ${fallbackNote}. Delete it and reinstall`);
			continue;
		}
		found = `${managedVersion}; from ${managedPath}, which the agent searches before PATH`;
	}
	if (!found) {
		for (const command of commands) {
			found = commandVersion(command);
			if (found) break;
		}
	}
	if (found) {
		pass(label, found);
	} else {
		warn(label, `not found in ${binDir} or on PATH; ${fallbackNote}. Install it for faster searches`);
	}
}

if (process.platform === "win32") {
	// Same search order as getShellConfig() in packages/coding-agent/src/utils/shell.ts.
	const bashCandidates = [];
	if (process.env.ProgramFiles) bashCandidates.push(join(process.env.ProgramFiles, "Git", "bin", "bash.exe"));
	if (process.env["ProgramFiles(x86)"])
		bashCandidates.push(join(process.env["ProgramFiles(x86)"], "Git", "bin", "bash.exe"));
	const gitBash =
		bashCandidates.find((candidate) => existsSync(candidate)) ?? (commandVersion("bash") ? "bash" : undefined);
	if (gitBash) {
		pass("bash for the agent", gitBash);
	} else {
		warn(
			"bash for the agent",
			"no bash found; pi's bash tool and the skills' .sh templates need Git for Windows (https://git-scm.com/download/win) or a shellPath in settings.json",
		);
	}
}

try {
	// Same entry as npm run sure:site-info, so the doctor reports whatever the loader
	// picked rather than second-guessing the search order.
	const siteInfo = JSON.parse(
		execFileSync(process.execPath, ["--import", "tsx", join(root, "scripts", "sure-site-info.ts"), "--json"], {
			cwd: root,
			encoding: "utf8",
			stdio: ["ignore", "pipe", "pipe"],
			timeout: 60_000,
		}),
	);
	if (siteInfo.configured) {
		pass("site policy", `source: ${siteInfo.source}; path: ${siteInfo.path}`);
	} else {
		warn("site policy", "not configured; commands that need site storage will refuse until one is in place");
	}
} catch (error) {
	// sure-site-info.ts prints the loader's own message on stderr; error.message only
	// carries the command line.
	const stderr = typeof error?.stderr === "string" ? error.stderr.trim() : "";
	const detail = stderr || (error instanceof Error ? error.message : String(error));
	warn("site policy", `could not be read: ${detail.split(/\r?\n/)[0]}`);
}

const authPath = join(agentDir, "auth.json");
const modelsPath = join(agentDir, "models.json");
if (existsSync(authPath)) {
	pass("Pi auth", `${authPath} exists`);
} else {
	warn("Pi auth", `missing ${authPath}; /sure_init can create or update provider auth`);
}
if (existsSync(modelsPath)) {
	pass("Pi models", `${modelsPath} exists`);
} else {
	warn("Pi models", `missing ${modelsPath}; create it when using an OpenAI-compatible API gateway`);
}

const defaultEngineRelPath = "sure/external/sure-evaluation";
const defaultEnginePath = join(root, "sure", "external", "sure-evaluation");
const engineOverride = process.env.SURE_EVALUATION_HOME;
if (engineOverride) {
	if (existsSync(engineOverride)) {
		pass("sure-evaluation override", engineOverride);
	} else {
		warn("sure-evaluation override", `${engineOverride} does not exist; unset SURE_EVALUATION_HOME or fix the path`);
	}
} else {
	const gitmodulesPath = join(root, ".gitmodules");
	const gitlink = runGit(["ls-files", "--stage", "--", defaultEngineRelPath]);
	const indexedCommit = gitlink?.startsWith("160000 ") ? gitlink.split(/\s+/)[1] : undefined;
	const moduleUrl = existsSync(gitmodulesPath)
		? runGit(["config", "--file", ".gitmodules", "--get", `submodule.${defaultEngineRelPath}.url`])
		: undefined;
	const moduleBranch = existsSync(gitmodulesPath)
		? runGit(["config", "--file", ".gitmodules", "--get", `submodule.${defaultEngineRelPath}.branch`])
		: undefined;
	const engineFilesReady =
		existsSync(defaultEnginePath) && existsSync(join(defaultEnginePath, "pyproject.toml"));
	const engineHead = engineFilesReady
		? runGit(["-c", `safe.directory=${defaultEnginePath}`, "rev-parse", "HEAD"], defaultEnginePath)
		: undefined;

	if (!existsSync(gitmodulesPath) || !indexedCommit) {
		const runtimeSpec = readJson(join(root, "sure", "runtime", "evaluation", "runtime.json"));
		const expectedCommit =
			typeof runtimeSpec?.engine_commit === "string" ? runtimeSpec.engine_commit : undefined;
		if (!engineFilesReady || !engineHead) {
			warn(
				"sure-evaluation checkout",
				"missing or not a readable Git checkout at sure/external/sure-evaluation",
			);
		} else if (expectedCommit && engineHead !== expectedCommit) {
			warn(
				"sure-evaluation checkout",
				`standalone checkout ${engineHead.slice(0, 12)} differs from runtime lock ${expectedCommit.slice(0, 12)}`,
			);
		} else {
			pass(
				"sure-evaluation checkout",
				`standalone Git checkout @ ${engineHead.slice(0, 12)}; runtime lock matches`,
			);
		}
	} else if (!engineFilesReady) {
		warn(
			"sure-evaluation submodule",
			"not initialized; run git submodule update --init --recursive",
		);
	} else {
		const headCommit = engineHead;
		const commitDetail = headCommit
			? `${headCommit.slice(0, 12)}${headCommit !== indexedCommit ? `; indexed ${indexedCommit.slice(0, 12)}` : ""}`
			: `indexed ${indexedCommit.slice(0, 12)}`;
		const locationDetail = `${moduleUrl ?? defaultEngineRelPath}${moduleBranch ? ` (${moduleBranch})` : ""}`;
		const runtimeSpec = readJson(join(root, "sure", "runtime", "evaluation", "runtime.json"));
		const lockedCommit =
			typeof runtimeSpec?.engine_commit === "string" ? runtimeSpec.engine_commit.trim() : undefined;
		const hasValidRuntimeLock = Boolean(lockedCommit && /^[0-9a-f]{40}$/i.test(lockedCommit));
		if (headCommit && headCommit !== indexedCommit) {
			warn(
				"sure-evaluation submodule",
				`${locationDetail} at ${commitDetail}; commit the gitlink after verification or reset the submodule`,
			);
		} else if (headCommit && !hasValidRuntimeLock) {
			warn(
				"sure-evaluation submodule",
				`${locationDetail} at ${headCommit.slice(0, 12)} but sure/runtime/evaluation/runtime.json has no valid engine_commit; /sure_eval cannot verify the evaluation engine until the runtime lock is restored`,
			);
		} else if (headCommit && headCommit !== lockedCommit) {
			// docs/evaluation_engine.md: /sure_eval refuses a route plan when the checkout and
			// runtime.json disagree. Comparing only the checkout against the gitlink reported
			// PASS in exactly this case, because both can be updated without the runtime lock.
			warn(
				"sure-evaluation submodule",
				`${locationDetail} at ${headCommit.slice(0, 12)} but runtime.json locks ${lockedCommit.slice(0, 12)}; /sure_eval fails with "evaluation engine commit differs from the locked runtime" until they agree`,
			);
		} else {
			pass("sure-evaluation submodule", `${locationDetail} @ ${commitDetail}`);
		}
	}
}

// --- runtime materialization ---------------------------------------------------------
//
// Every skill and /sure_eval need a materialized runtime, and "not materialized" is the
// most common reason a correct command fails while the doctor reports nothing. Both
// runtimes name their directory after a hash of their lock file, so the check rebuilds
// the id from the spec and the current lock, then looks for that directory. A lock change
// moves the id — exactly the case where the old directory is stale and a rebuild is due.

function sha256File(path) {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function runtimeId(kind, spec, lockSha) {
	const python = String(spec.python ?? "3.11").replaceAll(".", "");
	if (kind === "harness") {
		// bootstrap.py refuses a harness spec without these two, so surface the gap here
		// instead of building an id carrying "undefined".
		if (!spec.harness_version || !spec.materialization_version) return null;
		return `sure-harness-${spec.harness_version}-m${spec.materialization_version}-py${python}-${lockSha.slice(0, 12)}`;
	}
	const commit = String(spec.engine_commit ?? "").slice(0, 12);
	// evaluation_runtime.py defaults these two the same way; match it exactly.
	return `sure-evaluation-${spec.runtime_version ?? "root-v1"}-m${spec.materialization_version ?? 1}-${commit}-py${python}-${lockSha.slice(0, 12)}`;
}

for (const runtime of [
	{
		label: "harness runtime",
		spec: "sure/runtime/harness/runtime.json",
		dir: "sure/.runtime/harness",
		kind: "harness",
		prepare: "uv run --no-project --python 3.11 sure/runtime/harness/bootstrap.py --json",
	},
	{
		label: "evaluation runtime",
		spec: "sure/runtime/evaluation/runtime.json",
		dir: "sure/.runtime/evaluation",
		kind: "evaluation",
		prepare:
			"python3 sure/skills/sure_infer/scripts/evaluation_runtime.py --engine-root sure/external/sure-evaluation --prepare",
		prepareNote:
			"Preparing by hand needs a bound Harness Runtime first (any Sure run binds one); see docs/evaluation_engine.md.",
	},
]) {
	const specPath = join(root, runtime.spec);
	const spec = readJson(specPath);
	if (!spec) {
		warn(runtime.label, `${runtime.spec} is missing`);
		continue;
	}
	const lockPath = join(dirname(specPath), String(spec.lock_file ?? "requirements.lock.txt"));
	if (!existsSync(lockPath)) {
		warn(runtime.label, `lock file is missing: ${lockPath}`);
		continue;
	}
	const lockSha = sha256File(lockPath);
	const id = runtimeId(runtime.kind, spec, lockSha);
	if (!id) {
		warn(runtime.label, `${runtime.spec} must declare harness_version and materialization_version`);
		continue;
	}
	const runtimeDir = resolve(root, runtime.dir, id);
	const manifestPath = join(runtimeDir, "runtime-manifest.json");
	if (!existsSync(manifestPath)) {
		warn(
			runtime.label,
			`${id} is not materialized; the first Sure run prepares it.${runtime.prepareNote ? ` ${runtime.prepareNote}` : ""} To prepare now: ${runtime.prepare}`,
		);
		continue;
	}
	// A directory that exists is not a runtime. A partially copied or quarantined one keeps
	// its manifest, and resolve.ts rejects it and rebuilds — so compare what the manifest
	// claims against the spec and the lock, and require the interpreter it names. Still no
	// interpreter start: two reads and one existsSync.
	const manifest = readJson(manifestPath);
	const problems = [];
	if (!manifest) {
		problems.push("the manifest is not readable JSON");
	} else {
		if (manifest.runtime_id !== id) problems.push(`manifest runtime_id is ${manifest.runtime_id ?? "<missing>"}`);
		if (manifest.lock_sha256 !== lockSha) problems.push("manifest lock_sha256 does not match the lock");
		const interpreter = manifest.python_executable;
		if (typeof interpreter === "string" && interpreter) {
			const interpreterPath = isAbsolute(interpreter) ? resolve(interpreter) : resolve(runtimeDir, interpreter);
			if (!isInside(runtimeDir, interpreterPath) || !isRealPathInside(runtimeDir, interpreterPath)) {
				problems.push("manifest python_executable escapes the runtime directory");
			} else if (!isFile(interpreterPath)) {
				problems.push("the interpreter it names is missing");
			}
		}
		if (runtime.kind === "harness") {
			const baseRelative = manifest.base_python_relative;
			if (typeof baseRelative !== "string" || !baseRelative) {
				problems.push("manifest base_python_relative is missing");
			} else {
				const basePath = resolve(runtimeDir, baseRelative);
				if (!isInside(runtimeDir, basePath) || !isRealPathInside(runtimeDir, basePath)) {
					problems.push("manifest base_python_relative escapes the runtime directory");
				} else if (!isDirectory(basePath)) {
					problems.push("the base Python directory it names is missing");
				}
			}
		} else if (typeof interpreter !== "string" || !interpreter) {
			problems.push("manifest python_executable is missing");
		}
		const runtimeInterpreter = join(runtimeDir, process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "python.exe" : "python");
		if (!isFile(runtimeInterpreter)) problems.push("the runtime interpreter is missing");
		else if (!isRealPathInside(runtimeDir, runtimeInterpreter)) problems.push("the runtime interpreter escapes the runtime directory");
	}
	if (problems.length > 0) {
		warn(runtime.label, `${id} exists but is not usable (${problems.join("; ")}). To repair: ${runtime.prepare}`);
	} else {
		pass(runtime.label, `${id} is materialized`);
	}
}

const datasetRoot = process.env.SURE_EVAL_DATASETS_ROOT
	? join(process.env.SURE_EVAL_DATASETS_ROOT, "sure_benchmark", "jsonl")
	: join(root, "data", "datasets", "sure_benchmark", "jsonl");
if (existsSync(datasetRoot)) {
	const jsonlCount = readdirSync(datasetRoot).filter((name) => name.endsWith(".jsonl")).length;
	if (jsonlCount > 0) {
		pass("sure-eval datasets", `${datasetRoot} (${jsonlCount} jsonl files)`);
	} else {
		warn("sure-eval datasets", `${datasetRoot} exists but contains no .jsonl files`);
	}
} else {
	warn(
		"sure-eval datasets",
		`missing ${datasetRoot}; /sure_infer needs sure_benchmark/jsonl. Link it or set SURE_EVAL_DATASETS_ROOT`,
	);
}

const marks = {
	pass: "PASS",
	warn: "WARN",
	fail: "FAIL",
};

for (const check of checks) {
	console.log(`${marks[check.level]} ${check.name}: ${check.detail}`);
}

const failed = checks.filter((check) => check.level === "fail");
const warned = checks.filter((check) => check.level === "warn");
console.log("");
console.log(`SURE doctor summary: ${failed.length} failed, ${warned.length} warning(s), ${checks.length} total.`);

if (failed.length > 0) {
	console.log("");
	console.log("Recommended first fix:");
	console.log("  npm install --ignore-scripts");
	console.log("  npm run sure:doctor");
	process.exit(1);
}
