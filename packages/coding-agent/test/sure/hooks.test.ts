import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SureHookRunner } from "../../src/core/sure/hooks.ts";
import type { SureSkillPackage } from "../../src/core/sure/types.ts";

function packageWithHook(dir: string, source: string, handler = "preFinish"): SureSkillPackage {
	writeFileSync(join(dir, "hook.ts"), source);
	return {
		manifest: {
			name: "test-skill",
			hooks: { pre_finish: [{ module: "hook.ts", handler }] },
		} as unknown as SureSkillPackage["manifest"],
		manifestPath: join(dir, "sure.skill.json"),
		packageDir: dir,
		promptPath: join(dir, "SKILL.md"),
		prompt: "",
		source: "project",
		sourceRoot: dir,
	};
}

describe("SureHookRunner deadline", () => {
	let dir: string;

	beforeEach(() => {
		dir = mkdtempSync(join(tmpdir(), "sure-hooks-"));
	});

	afterEach(() => {
		vi.unstubAllEnvs();
		rmSync(dir, { recursive: true, force: true });
	});

	// A hanging pre_tool_call used to block the run with no way out. The deadline turns it
	// into an ordinary gate failure; it does not kill the handler (issue #172).
	test("a hook that never returns fails the gate instead of blocking the run", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "500");
		const runner = new SureHookRunner(
			packageWithHook(
				dir,
				`export async function preFinish() {
					await new Promise(() => {});
					return { ok: true };
				}`,
			),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(false);
		expect(result.message).toContain("did not return within 500ms");
		expect(result.repair).toContain("SURE_HOOK_TIMEOUT_MS");
	});

	// The deadline covers loading the hook module, not only calling its handler: top-level
	// code that never settles used to block the run exactly like a hanging handler.
	test("a hook module that hangs at import fails the gate instead of blocking the run", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "500");
		const runner = new SureHookRunner(
			packageWithHook(
				dir,
				`await new Promise(() => {});
				export function preFinish() { return { ok: true }; }`,
			),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(false);
		expect(result.message).toContain("did not return within 500ms");
	});

	test("a fast hook still passes", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "5000");
		const runner = new SureHookRunner(packageWithHook(dir, `export function preFinish() { return { ok: true }; }`));

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(true);
	});

	// parseInt("2m") is 2, which failed every hook carrying an await within 2ms while the
	// report named a deadline nobody set.
	test("a SURE_HOOK_TIMEOUT_MS that is not a plain count falls back to the default", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "2m");
		const runner = new SureHookRunner(
			packageWithHook(
				dir,
				`export async function preFinish() {
					await new Promise((resolve) => setTimeout(resolve, 10));
					return { ok: true };
				}`,
			),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(true);
	});

	// setTimeout treats a delay above 2^31-1 as 1ms, which failed every hook at once.
	test("an out-of-range SURE_HOOK_TIMEOUT_MS is clamped instead of firing immediately", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "999999999999");
		const runner = new SureHookRunner(
			packageWithHook(
				dir,
				`export async function preFinish() {
					await new Promise((resolve) => setTimeout(resolve, 10));
					return { ok: true };
				}`,
			),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(true);
	});

	// A synchronous loop is the case a soft deadline cannot touch: only terminating the
	// worker (issue #172) stops it, and the run must go on afterwards.
	test("a hook stuck in a synchronous loop is stopped when its deadline expires", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "500");
		const runner = new SureHookRunner(packageWithHook(dir, `export function preFinish() { while (true) {} }`));

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(false);
		expect(result.message).toContain("did not return within 500ms");
		expect(result.repair).toContain("has been stopped");
	});

	test("the next hook runs after a deadline killed the worker", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "500");
		const runner = new SureHookRunner(packageWithHook(dir, `export function preFinish() { while (true) {} }`));
		await runner.run("pre_finish", {} as never);

		const runner2 = new SureHookRunner(packageWithHook(dir, `export function preFinish() { return { ok: true }; }`));
		const result = await runner2.run("pre_finish", {} as never);

		expect(result.ok).toBe(true);
	});

	test("a hook cannot read the credentials of the agent process", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "5000");
		vi.stubEnv("PI_TEST_SECRET", "hunter2");
		const runner = new SureHookRunner(
			packageWithHook(
				dir,
				`export function preFinish() {
					return { ok: true, state_patch: { secret: process.env.PI_TEST_SECRET ?? "absent" } };
				}`,
			),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(true);
		expect((result.state_patch as { secret: string }).secret).toBe("absent");
	});

	test("a hook cannot pollute the environment another skill's hooks see", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "5000");
		const writer = new SureHookRunner(
			packageWithHook(
				dir,
				`export function preFinish() {
					process.env.SURE_HOOK_DIRTY = "yes";
					return { ok: true };
				}`,
			),
		);
		await writer.run("pre_finish", {} as never);

		const reader = new SureHookRunner(
			packageWithHook(
				dir,
				`export function preFinish() {
					return { ok: true, state_patch: { dirty: process.env.SURE_HOOK_DIRTY ?? "absent" } };
				}`,
			),
		);
		const result = await reader.run("pre_finish", {} as never);

		expect((result.state_patch as { dirty: string }).dirty).toBe("absent");
	});

	test("a hook that rejects still reports its own message", async () => {
		vi.stubEnv("SURE_HOOK_TIMEOUT_MS", "5000");
		const runner = new SureHookRunner(
			packageWithHook(dir, `export async function preFinish() { throw new Error("artifact missing"); }`),
		);

		const result = await runner.run("pre_finish", {} as never);

		expect(result.ok).toBe(false);
		expect(result.message).toContain("artifact missing");
		expect(result.message).not.toContain("did not return within");
	});
});
