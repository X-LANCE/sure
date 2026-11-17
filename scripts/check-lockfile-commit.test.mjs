import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const script = resolve(import.meta.dirname, "check-lockfile-commit.mjs");

const BASE = {
	"": { name: "pi-monorepo", version: "0.0.3", workspaces: ["packages/ai"] },
	"node_modules/@earendil-works/pi-ai": { resolved: "packages/ai", link: true },
	"packages/ai": { name: "@earendil-works/pi-ai", version: "0.0.3" },
};

// npm run check runs inside the pre-commit hook, where git exports GIT_DIR and
// GIT_INDEX_FILE for the user's repository; they must not reach the temp repo.
function isolatedEnv() {
	const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("GIT_")));
	delete env.PI_ALLOW_LOCKFILE_CHANGE;
	return env;
}

// Commits BASE as package-lock.json, stages `packages` over it, and runs the hook.
function runWithStagedLock(packages) {
	const repo = mkdtempSync(join(tmpdir(), "sure-lockfile-commit-"));
	const env = isolatedEnv();
	const git = (...args) =>
		assert.equal(
			spawnSync(
				"git",
				["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", ...args],
				{ cwd: repo, env },
			).status,
			0,
		);
	try {
		git("init", "--quiet");
		writeFileSync(join(repo, "package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: BASE }));
		git("add", "package-lock.json");
		git("commit", "--quiet", "-m", "base");
		writeFileSync(join(repo, "package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages }));
		git("add", "package-lock.json");
		return spawnSync(process.execPath, [script], { cwd: repo, encoding: "utf8", env });
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
}

test("allows a workspace version bump", () => {
	const result = runWithStagedLock({ ...BASE, "packages/ai": { name: "@earendil-works/pi-ai", version: "0.0.4" } });
	assert.equal(result.status, 0, result.stderr);
	assert.match(result.stderr, /only updates workspace package metadata/);
});

// npm nests a dependency under the workspace when its version conflicts with the
// hoisted one, so the path starts with packages/ but the entry is an external package.
test("blocks a dependency nested under a workspace", () => {
	const result = runWithStagedLock({
		...BASE,
		"packages/ai/node_modules/left-pad": { version: "1.3.0", resolved: "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz" },
	});
	assert.equal(result.status, 1, result.stderr);
	assert.match(result.stderr, /added left-pad@1\.3\.0/);
});

test("leaves the repository named by the hook environment alone", () => {
	const outer = mkdtempSync(join(tmpdir(), "sure-lockfile-outer-"));
	const saved = { GIT_DIR: process.env.GIT_DIR, GIT_INDEX_FILE: process.env.GIT_INDEX_FILE };
	process.env.GIT_DIR = join(outer, "git");
	process.env.GIT_INDEX_FILE = join(outer, "index");
	try {
		const result = runWithStagedLock({ ...BASE, "packages/ai": { name: "@earendil-works/pi-ai", version: "0.0.4" } });
		assert.equal(result.status, 0, result.stderr);
		assert.equal(existsSync(process.env.GIT_DIR), false);
		assert.equal(existsSync(process.env.GIT_INDEX_FILE), false);
	} finally {
		for (const [key, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
		rmSync(outer, { recursive: true, force: true });
	}
});
