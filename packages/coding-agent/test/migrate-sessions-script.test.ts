import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { getShellConfig } from "../src/utils/shell.ts";

const SCRIPT = join(__dirname, "..", "scripts", "migrate-sessions.sh");

function writeLegacySession(agentDir: string, name: string, cwd: string): void {
	mkdirSync(agentDir, { recursive: true });
	writeFileSync(
		join(agentDir, name),
		`${JSON.stringify({ type: "session", id: name, timestamp: "2025-12-01T10:00:00.000Z", cwd })}\n`,
	);
}

function runScript(env: Record<string, string>) {
	return spawnSync(getShellConfig().shell, [SCRIPT, "--dry-run"], {
		encoding: "utf-8",
		env: { ...process.env, ...env },
	});
}

describe("migrate-sessions.sh", () => {
	let home: string;

	afterEach(() => {
		rmSync(home, { recursive: true, force: true });
	});

	it("goes through every session file instead of stopping after the first", () => {
		home = mkdtempSync(join(tmpdir(), "migrate-sessions-"));
		const agentDir = join(home, ".pi", "agent");
		writeLegacySession(agentDir, "first.jsonl", "/home/dev/project-a");
		writeLegacySession(agentDir, "second.jsonl", "/home/dev/project-b");

		const result = runScript({ HOME: home });

		expect(result.status).toBe(0);
		expect(result.stdout).toContain("first.jsonl");
		expect(result.stdout).toContain("second.jsonl");
		expect(result.stdout).toContain("---");
	});

	it("reads the agent dir from PI_CODING_AGENT_DIR like the CLI does", () => {
		home = mkdtempSync(join(tmpdir(), "migrate-sessions-"));
		const agentDir = join(home, "custom-agent");
		writeLegacySession(agentDir, "first.jsonl", "/home/dev/project-a");

		const result = runScript({ HOME: home, PI_CODING_AGENT_DIR: agentDir });

		expect(result.status).toBe(0);
		expect(result.stdout).toContain("Found 1 session file(s)");
	});
});
