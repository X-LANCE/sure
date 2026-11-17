import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

// An events.jsonl over V8's string limit (~512 MiB) cannot be read as one string: readFileSync
// with an encoding throws ERR_STRING_TOO_LONG. Allocating that much in a test is not on, so the
// mock throws the same error for any encoded read of events.jsonl and the file stays small.
vi.mock("node:fs", async (importOriginal) => {
	const actual = await importOriginal<typeof import("node:fs")>();
	const readFileSync = ((path: Parameters<typeof actual.readFileSync>[0], options?: unknown) => {
		const encoded =
			typeof options === "string" ||
			(typeof options === "object" && options !== null && "encoding" in options && options.encoding);
		if (encoded && String(path).endsWith("events.jsonl")) {
			throw Object.assign(new Error("Cannot create a string longer than 0x1fffffe8 characters"), {
				code: "ERR_STRING_TOO_LONG",
			});
		}
		return actual.readFileSync(path, options as Parameters<typeof actual.readFileSync>[1]);
	}) as typeof actual.readFileSync;
	return { ...actual, readFileSync };
});

const { eventsSince, readEventCount } = await import("../../../../sure/runtime/memory/match.ts");

const TMP = resolve(__dirname, "tmp-memory-events-large");

describe("memory match: events.jsonl too large for one string", () => {
	const runDir = join(TMP, "run-events");

	beforeEach(() => {
		rmSync(runDir, { recursive: true, force: true });
		mkdirSync(runDir, { recursive: true });
	});

	it("still counts lines and returns the events after the cutoff", () => {
		const lines = [
			JSON.stringify({ type: "created", timestamp: "t0", data: {} }),
			JSON.stringify({
				type: "tool_call",
				timestamp: "t1",
				data: { toolName: "bash", input: { command: "cat x" } },
			}),
			"",
			JSON.stringify({ type: "tool_result", timestamp: "t2", data: { toolName: "bash", isError: false } }),
		];
		writeFileSync(join(runDir, "events.jsonl"), `${lines.join("\n")}\n{"type":"torn`, "utf-8");
		expect(readEventCount(runDir)).toBe(4);
		expect((eventsSince(runDir, 1) as { type: string }[]).map((event) => event.type)).toEqual([
			"tool_call",
			"tool_result",
		]);
		expect(eventsSince(runDir, 4)).toEqual([]);
	});
});
