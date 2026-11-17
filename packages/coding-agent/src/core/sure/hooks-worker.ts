/**
 * The hook execution boundary (issue #172). Each skill's hooks run in their own
 * worker thread, so a hook cannot read the environment the agent runs with, cannot
 * pollute globals that hooks of another skill would see, and is really stopped when
 * its deadline expires: the runner terminates this worker and starts a fresh one
 * for the next call. What this does not confine is the filesystem — a hook is still
 * ordinary code with ordinary file access, which needs the separate-process design.
 */
import { parentPort } from "node:worker_threads";
import { createJiti } from "jiti/static";
import { type HookFunction, normalizeHookResult } from "./hooks.ts";
import { getSureHookAliases } from "./module-loader.ts";
import type { SureHookContext, SureHookPoint } from "./types.ts";

interface HookCall {
	id: number;
	hookPath: string;
	handlerName: string;
	declaration: { module: string };
	point: SureHookPoint;
	context: Omit<SureHookContext, "point">;
}

export type HookReply =
	| { type: "ready" }
	| { type: "result"; id: number; result: ReturnType<typeof normalizeHookResult> }
	| { type: "error"; id: number; message: string };

const port = parentPort;
if (!port) {
	throw new Error("hooks-worker must run on a worker thread");
}

// Node gives workers a throwing stub for process.chdir, which defeats cross-spawn's
// worker guard (it checks for `undefined` before switching cwd to probe a command).
// Unsetting it restores the guard: which resolves PATH and absolute commands without
// a cwd switch, which is everything the hooks spawn.
process.chdir = undefined as never;

const jiti = createJiti(import.meta.url, {
	moduleCache: false,
	alias: getSureHookAliases(),
});

port.on("message", async (call: HookCall) => {
	try {
		const loaded = await jiti.import(call.hookPath);
		const moduleValue = loaded as Record<string, unknown>;
		const handler =
			(typeof moduleValue[call.handlerName] === "function" ? moduleValue[call.handlerName] : undefined) ??
			(typeof moduleValue.default === "function" ? moduleValue.default : undefined);
		if (!handler) {
			port.postMessage({
				type: "result",
				id: call.id,
				result: {
					ok: false,
					message: `Hook handler not found: ${call.handlerName}`,
					repair: `Export a function named "${call.handlerName}" from ${call.declaration.module}, or provide a default export.`,
				},
			} satisfies HookReply);
			return;
		}
		const value = await (handler as HookFunction)({
			...call.context,
			point: call.point,
		});
		const result = normalizeHookResult(value);
		try {
			port.postMessage({ type: "result", id: call.id, result } satisfies HookReply);
		} catch {
			// A handler returned something the thread boundary cannot carry.
			port.postMessage({
				type: "result",
				id: call.id,
				result: {
					ok: result.ok ?? false,
					message: result.message ?? "hook result could not cross the thread boundary",
					repair: "Return plain JSON data from the hook handler.",
				},
			} satisfies HookReply);
		}
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		port.postMessage({ type: "error", id: call.id, message } satisfies HookReply);
	}
});

port.postMessage({ type: "ready" } satisfies HookReply);
