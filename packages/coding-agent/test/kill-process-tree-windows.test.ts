import type { ChildProcess } from "node:child_process";
import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { join } from "node:path";
import { Readable } from "node:stream";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { spawnMock, original } = vi.hoisted(() => ({
	spawnMock: vi.fn(),
	original: {} as { spawn?: typeof import("node:child_process").spawn },
}));

vi.mock("child_process", async (importOriginal) => {
	const actual = await importOriginal<typeof import("child_process")>();
	original.spawn = actual.spawn;
	return { ...actual, spawn: spawnMock };
});

import { killProcessTree } from "../src/utils/shell.ts";

const taskkillPath = join(process.env.SystemRoot ?? "C:\\Windows", "System32", "taskkill.exe");

// A child that spawns a detached grandchild and exits immediately. Once the child is gone the
// grandchild is a live process whose parent pid is dead - the same state a descendant ends up in
// when it is spawned while taskkill /T is sweeping the tree.
const SPAWN_ORPHANED_GRANDCHILD = [
	"const { spawn } = require('child_process');",
	"const g = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)'], { detached: true, stdio: 'ignore' });",
	"g.unref();",
	"console.log(g.pid);",
].join("");

function isAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

async function waitForExit(pid: number, timeoutMs: number): Promise<boolean> {
	const deadline = Date.now() + timeoutMs;
	while (isAlive(pid) && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
	return !isAlive(pid);
}

/** Runs a child that leaves one detached process behind, and returns [childPid, grandchildPid]. */
function startOrphanedGrandchild(): [number, number] {
	const child = spawnSync(process.execPath, ["-e", SPAWN_ORPHANED_GRANDCHILD], {
		encoding: "utf-8",
		windowsHide: true,
	});
	return [child.pid ?? 0, Number(child.stdout.trim())];
}

/** Stands in for the PowerShell process-table read, so a round can be handed a table we choose. */
function fakeProcessTableRead(lines: string[]): ChildProcess {
	const stdout = new Readable({
		read() {
			this.push(lines.join("\r\n"));
			this.push(null);
		},
	});
	const child = new EventEmitter() as ChildProcess;
	Object.assign(child, { stdout, kill: () => true });
	stdout.once("end", () => child.emit("close", 0));
	return child;
}

// Windows only: the POSIX branch signals the whole process group in one syscall, so it has no
// userspace tree snapshot that a newly spawned descendant can slip past.
describe.skipIf(process.platform !== "win32")("killProcessTree on Windows", () => {
	let startedPids: number[] = [];

	beforeEach(() => {
		// Pass every spawn through to the real one unless a test says otherwise.
		spawnMock.mockImplementation((...args: Parameters<typeof import("node:child_process").spawn>) =>
			original.spawn?.(...args),
		);
	});

	afterEach(() => {
		// Only ever clean up processes this test started.
		for (const pid of startedPids) {
			spawnSync(taskkillPath, ["/F", "/PID", String(pid)], { stdio: "ignore" });
		}
		startedPids = [];
		spawnMock.mockReset();
	});

	it("kills a descendant that outlived the process tree taskkill swept", async () => {
		const [childPid, grandchildPid] = startOrphanedGrandchild();
		startedPids.push(grandchildPid);
		expect(Number.isInteger(grandchildPid)).toBe(true);
		expect(isAlive(grandchildPid)).toBe(true);

		await killProcessTree(childPid);

		// Measured on this host: taskkill dispatch -> target gone takes 158 ms (median of 10, max
		// 173 ms), so a grandchild still running seconds later was never targeted at all.
		expect(await waitForExit(grandchildPid, 5000)).toBe(true);
	});

	it("leaves a pid alone once it names a different process than the one we killed", async () => {
		// A bystander: a live process that belongs to nobody's tree and must not be touched.
		const [, bystanderPid] = startOrphanedGrandchild();
		startedPids.push(bystanderPid);
		expect(isAlive(bystanderPid)).toBe(true);

		// Windows only ever issues pids that are multiples of four, so this one cannot be in use:
		// the kill we dispatch against it is guaranteed to hit nothing real.
		const reissuedPid = 999999997;
		// The table the confirmation round reads: the pid we were told to kill is alive again, but
		// with a creation time later than the kill - the number has been handed to someone else -
		// and the bystander is that someone else's child.
		const reusedAt = Date.now() + 60000;
		spawnMock.mockImplementation(
			(...args: Parameters<typeof import("node:child_process").spawn>): ChildProcess | undefined => {
				if (String(args[0]).toLowerCase().includes("powershell")) {
					return fakeProcessTableRead([
						`${reissuedPid} 4 ${reusedAt}`,
						`${bystanderPid} ${reissuedPid} ${reusedAt}`,
					]);
				}
				return original.spawn?.(...args);
			},
		);

		await killProcessTree(reissuedPid);

		expect(isAlive(bystanderPid)).toBe(true);
	});

	it("leaves a process alone whose parent pid only names our tree by reuse", async () => {
		// A bystander whose own parent died long ago: Windows keeps the dead parent's number in
		// ParentProcessId, and that number has since been handed to the shell we spawned.
		const [, bystanderPid] = startOrphanedGrandchild();
		startedPids.push(bystanderPid);
		expect(isAlive(bystanderPid)).toBe(true);

		const killedPid = 999999997;
		const spawnedAt = Date.now();
		// The confirmation round finds the shell already gone and the bystander, created before
		// the shell existed, still pointing at its pid.
		spawnMock.mockImplementation(
			(...args: Parameters<typeof import("node:child_process").spawn>): ChildProcess | undefined => {
				if (String(args[0]).toLowerCase().includes("powershell")) {
					return fakeProcessTableRead([`${bystanderPid} ${killedPid} ${spawnedAt - 60000}`]);
				}
				return original.spawn?.(...args);
			},
		);

		await killProcessTree(killedPid, spawnedAt);

		expect(isAlive(bystanderPid)).toBe(true);
	});

	it("still kills a child whose recorded creation time reads a few ms before the spawn", async () => {
		// The kernel's CreationDate ticks at ~15.6 ms and comes from a different clock than
		// Date.now(), so a child started right after the shell can read as slightly older.
		const [, childPid] = startOrphanedGrandchild();
		startedPids.push(childPid);
		expect(isAlive(childPid)).toBe(true);

		const killedPid = 999999997;
		const spawnedAt = Date.now();
		spawnMock.mockImplementation(
			(...args: Parameters<typeof import("node:child_process").spawn>): ChildProcess | undefined => {
				if (String(args[0]).toLowerCase().includes("powershell")) {
					return fakeProcessTableRead([`${childPid} ${killedPid} ${spawnedAt - 5}`]);
				}
				return original.spawn?.(...args);
			},
		);

		await killProcessTree(killedPid, spawnedAt);

		expect(await waitForExit(childPid, 5000)).toBe(true);
	});
});
