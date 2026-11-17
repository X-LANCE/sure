import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const script = resolve(import.meta.dirname, "check-pinned-deps.mjs");

// A two-workspace repository shaped like this one: the agent depends on the
// in-repo pi-ai and on pi-tui, which comes from the npm registry.
function run(dependencies) {
	const repo = mkdtempSync(join(tmpdir(), "sure-pinned-deps-"));
	const write = (path, value) => {
		mkdirSync(join(repo, path), { recursive: true });
		writeFileSync(join(repo, path, "package.json"), JSON.stringify(value));
	};
	try {
		write(".", { name: "root", private: true, workspaces: ["packages/ai", "packages/coding-agent"] });
		write("packages/ai", { name: "@earendil-works/pi-ai", version: "0.0.3" });
		write("packages/coding-agent", { name: "@earendil-works/pi-coding-agent", dependencies });
		return spawnSync(process.execPath, [script], { cwd: repo, encoding: "utf8" });
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
}

test("accepts a range on a workspace package and exact external pins", () => {
	const result = run({ "@earendil-works/pi-ai": "^0.0.3", "@earendil-works/pi-tui": "0.85.1" });
	assert.equal(result.status, 0, result.stderr);
});

test("rejects a range on an external package that shares the workspace prefix", () => {
	const result = run({ "@earendil-works/pi-ai": "0.0.3", "@earendil-works/pi-tui": "^0.85.1" });
	assert.equal(result.status, 1, result.stderr);
	assert.match(result.stderr, /dependencies\.@earendil-works\/pi-tui must be pinned, found \^0\.85\.1/);
});
