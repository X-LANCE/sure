import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const script = resolve(import.meta.dirname, "check-fixture-provenance.mjs");

// Mono 16 kHz 16-bit PCM with no samples: the smallest WAV the check accepts.
function wav() {
	const header = Buffer.alloc(44);
	header.write("RIFF", 0, "ascii");
	header.writeUInt32LE(36, 4);
	header.write("WAVEfmt ", 8, "ascii");
	header.writeUInt32LE(16, 16);
	header.writeUInt16LE(1, 20);
	header.writeUInt16LE(1, 22);
	header.writeUInt32LE(16_000, 24);
	header.writeUInt32LE(32_000, 28);
	header.writeUInt16LE(2, 32);
	header.writeUInt16LE(16, 34);
	header.write("data", 36, "ascii");
	return header;
}

// The check resolves its repository root from its own location, so it runs from a copy.
test("accepts audio declared in a fixture subdirectory", () => {
	const repo = mkdtempSync(join(tmpdir(), "sure-fixture-provenance-"));
	try {
		mkdirSync(join(repo, "scripts"));
		copyFileSync(script, join(repo, "scripts", "check-fixture-provenance.mjs"));
		mkdirSync(join(repo, "sure", "runtime", "evaluation"), { recursive: true });
		writeFileSync(join(repo, "sure", "runtime", "evaluation", "harness-task-profiles.json"), '{"tasks":{}}');
		const fixture = join(repo, "fixtures", "tasks", "asr", "demo");
		mkdirSync(join(fixture, "audio"), { recursive: true });
		writeFileSync(join(fixture, "audio", "a.wav"), wav());
		const provenance = {
			schema: "sure.fixture_provenance.v1",
			dataset: {
				repository: "example/demo",
				revision: "main",
				configuration: "default",
				split: "test",
				metadata_sha256: "0".repeat(64),
				license: "CC-BY-4.0",
				license_url: "https://example.invalid/license",
				source_url: "https://example.invalid/source",
				citation_url: "https://example.invalid/citation",
			},
			selection: { sentence_ids: ["a"] },
			files: [
				{
					path: "audio/a.wav",
					source_path: "a.wav",
					sha256: createHash("sha256").update(wav()).digest("hex"),
				},
			],
		};
		writeFileSync(join(fixture, "provenance.json"), JSON.stringify(provenance));
		const result = spawnSync(process.execPath, [join(repo, "scripts", "check-fixture-provenance.mjs")], {
			cwd: repo,
			encoding: "utf8",
		});
		assert.equal(result.status, 0, result.stderr);
	} finally {
		rmSync(repo, { recursive: true, force: true });
	}
});
