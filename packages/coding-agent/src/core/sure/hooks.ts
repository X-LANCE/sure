import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Worker } from "node:worker_threads";
import type { HookReply } from "./hooks-worker.ts";
import { resolveSurePackagePath } from "./manifest.ts";
import type { SureHookContext, SureHookDeclaration, SureHookPoint, SureHookResult, SureSkillPackage } from "./types.ts";

export type HookFunction = (
	context: SureHookContext,
) => SureHookResult | Promise<SureHookResult | undefined> | undefined;

/**
 * Deadline for one hook call, enforced by terminating the skill's hook worker
 * (issue #172). The handler does not survive it: a hook stuck in a synchronous
 * loop is as gone as one that hangs on I/O, and the next call starts a fresh
 * worker. `SURE_HOOK_TIMEOUT_MS` overrides the wait.
 */
const DEFAULT_HOOK_TIMEOUT_MS = 120_000;
// setTimeout treats any delay above 2^31-1 as 1ms and warns, so a huge value must clamp.
const MAX_HOOK_TIMEOUT_MS = 2_147_483_647;

function hookTimeoutMs(): number {
	const raw = process.env.SURE_HOOK_TIMEOUT_MS ?? "";
	// parseInt("2m") is 2; only a plain count of milliseconds is accepted.
	if (!/^\d+$/.test(raw)) return DEFAULT_HOOK_TIMEOUT_MS;
	const parsed = Number.parseInt(raw, 10);
	return parsed > 0 ? Math.min(parsed, MAX_HOOK_TIMEOUT_MS) : DEFAULT_HOOK_TIMEOUT_MS;
}

class HookDeadlineExceeded extends Error {}

async function withDeadline<T>(task: Promise<T> | T, ms: number): Promise<T> {
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		return await Promise.race([
			Promise.resolve(task),
			new Promise<never>((_, reject) => {
				timer = setTimeout(() => reject(new HookDeadlineExceeded(`exceeded ${ms}ms`)), ms);
				(timer as unknown as { unref?: () => void }).unref?.();
			}),
		]);
	} finally {
		if (timer) clearTimeout(timer);
	}
}

/** The agent's environment minus everything a hook has no business reading. */
const SENSITIVE_ENV_PATTERN =
	/^(PI_|OPENAI_|ANTHROPIC_|AZURE_|AWS_|GEMINI_|GOOGLE_)|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH/i;

function filterHookEnv(env: NodeJS.ProcessEnv): Record<string, string> {
	const filtered: Record<string, string> = {};
	for (const [name, value] of Object.entries(env)) {
		if (typeof value === "string" && !SENSITIVE_ENV_PATTERN.test(name)) {
			filtered[name] = value;
		}
	}
	return filtered;
}

export interface SureGateResult {
	ok: boolean;
	message?: string;
	repair?: string;
	diagnostics?: unknown;
	state_patch?: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

export function normalizeHookResult(value: unknown): SureHookResult {
	if (!isRecord(value)) {
		return {};
	}
	return {
		ok: typeof value.ok === "boolean" ? value.ok : undefined,
		message: typeof value.message === "string" ? value.message : undefined,
		repair: typeof value.repair === "string" ? value.repair : undefined,
		patch: value.patch,
		artifacts: value.artifacts,
		diagnostics: value.diagnostics,
		state_patch: value.state_patch,
	};
}

function toGateFailure(result: SureHookResult, fallbackMessage: string): SureGateResult | undefined {
	if (result.ok !== false && !result.repair) {
		return undefined;
	}
	return {
		ok: false,
		message: result.message ?? fallbackMessage,
		repair: result.repair ?? result.message ?? fallbackMessage,
		diagnostics: result.diagnostics,
		state_patch: result.state_patch,
	};
}

interface PendingCall {
	resolve: (result: SureHookResult) => void;
	reject: (error: Error) => void;
}

export class SureHookRunner {
	private skillPackage: SureSkillPackage;
	private worker: Worker | undefined;
	private workerReady: Promise<void> | undefined;
	private nextCallId = 1;
	private pending = new Map<number, PendingCall>();

	constructor(skillPackage: SureSkillPackage) {
		this.skillPackage = skillPackage;
	}

	async run(point: SureHookPoint, context: Omit<SureHookContext, "point">): Promise<SureGateResult> {
		const declarations = this.skillPackage.manifest.hooks?.[point] ?? [];
		let statePatch: unknown;
		for (const declaration of declarations) {
			const result = await this.runHook(point, declaration, context);
			if (result.state_patch !== undefined) {
				statePatch = result.state_patch;
			}
			const failure = toGateFailure(result, `Sure ${point} hook rejected the action.`);
			if (failure) {
				return { ...failure, state_patch: failure.state_patch ?? statePatch };
			}
		}
		return { ok: true, state_patch: statePatch };
	}

	private async runHook(
		point: SureHookPoint,
		declaration: SureHookDeclaration,
		context: Omit<SureHookContext, "point">,
	): Promise<SureHookResult> {
		const hookPath = resolveSurePackagePath(this.skillPackage.packageDir, declaration.module);
		if (!hookPath) {
			return {
				ok: false,
				message: `Hook module path escapes skill package: ${declaration.module}`,
				repair: "Fix sure.skill.json so hook module paths stay inside the skill package.",
			};
		}

		const timeoutMs = hookTimeoutMs();
		try {
			// The deadline covers the worker start and the module load as well: wherever
			// the hook stalls, expiry terminates the worker and the handler with it.
			return await withDeadline(this.callWorker(hookPath, declaration, point, context), timeoutMs);
		} catch (error) {
			if (error instanceof HookDeadlineExceeded) {
				this.dispose();
				return {
					ok: false,
					message: `Sure ${point} hook ${declaration.module} did not return within ${timeoutMs}ms`,
					repair: `Make the ${point} hook return faster, or raise SURE_HOOK_TIMEOUT_MS. The hook worker has been stopped.`,
				};
			}
			const message = error instanceof Error ? error.message : String(error);
			return {
				ok: false,
				message: `Sure ${point} hook failed: ${message}`,
				repair: `Fix the hook failure in ${declaration.module} and retry the action.`,
			};
		}
	}

	private callWorker(
		hookPath: string,
		declaration: SureHookDeclaration,
		point: SureHookPoint,
		context: Omit<SureHookContext, "point">,
	): Promise<SureHookResult> {
		return this.ensureWorker().then(
			(worker) =>
				new Promise<SureHookResult>((resolve, reject) => {
					const id = this.nextCallId++;
					this.pending.set(id, { resolve, reject });
					worker.ref();
					worker.postMessage({
						id,
						hookPath,
						handlerName: declaration.handler ?? point,
						declaration,
						point,
						context,
					});
				}),
		);
	}

	private ensureWorker(): Promise<Worker> {
		if (this.worker && this.workerReady) {
			return this.workerReady.then(() => this.worker as Worker);
		}
		// src runs the .ts entry and the built package the .js one, same as module-loader.
		const entry = [new URL("./hooks-worker.js", import.meta.url), new URL("./hooks-worker.ts", import.meta.url)]
			.map((url) => fileURLToPath(url))
			.find(existsSync);
		if (!entry) {
			return Promise.reject(new Error("hook worker entry not found beside hooks.ts"));
		}
		const worker = new Worker(entry, {
			// The hook runs without the agent's environment: provider keys and PI_*
			// variables are exactly what issue #172 keeps out of hook reach.
			env: filterHookEnv(process.env),
		});
		this.worker = worker;
		const ready = new Promise<void>((resolve, reject) => {
			worker.on("message", (message: HookReply) => {
				if (message?.type === "ready") {
					resolve();
					return;
				}
				if (message?.type === "result" || message?.type === "error") {
					const pending = this.pending.get(message.id);
					this.pending.delete(message.id);
					// A live worker would keep the CLI process alive after the run settles;
					// it only counts towards the event loop while a call is in flight.
					if (this.pending.size === 0) {
						worker.unref();
					}
					if (!pending) return;
					if (message.type === "result") {
						pending.resolve(message.result);
					} else {
						pending.reject(new Error(message.message));
					}
				}
			});
			const fail = (error: Error) => {
				if (this.worker !== worker) return;
				this.worker = undefined;
				this.workerReady = undefined;
				for (const { reject: rejectPending } of this.pending.values()) rejectPending(error);
				this.pending.clear();
				reject(error);
			};
			worker.on("error", fail);
			worker.on("exit", (code) => fail(new Error(`hook worker exited with code ${code}`)));
		});
		this.workerReady = ready;
		return ready.then(() => worker);
	}

	/** Stops the hook worker; the next hook call starts a fresh one. */
	dispose(): void {
		const worker = this.worker;
		this.worker = undefined;
		this.workerReady = undefined;
		for (const { reject } of this.pending.values()) reject(new Error("hook worker stopped"));
		this.pending.clear();
		void worker?.terminate();
	}
}
