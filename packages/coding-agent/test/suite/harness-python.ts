import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { repoRootForPackage, resolveHarnessPython } from "../../../../sure/runtime/harness/resolve.ts";

const memo = new Map<string, string>();

// resolveHarnessPython may run a bootstrap of up to 15 minutes when nothing is materialised, so only
// call it when a manifest for the current lock already exists; otherwise the fallback is immediate.
function runtimeIsMaterialised(repoRoot: string): boolean {
	try {
		const lock = readFileSync(resolve(repoRoot, "sure/runtime/harness/requirements.lock.txt"));
		const lockSha = createHash("sha256").update(lock).digest("hex");
		const base = resolve(repoRoot, "sure/.runtime/harness");
		return readdirSync(base).some((name) => {
			const manifest = resolve(base, name, "runtime-manifest.json");
			return existsSync(manifest) && JSON.parse(readFileSync(manifest, "utf-8")).lock_sha256 === lockSha;
		});
	} catch {
		return false;
	}
}

/**
 * The interpreter the product runs skill scripts with: the materialized Harness Runtime, which
 * carries PyYAML and the other locked dependencies. A bare system python only has them by
 * accident (a user-site install is invisible under the scrubbed test environment), so tests that
 * spawn scripts importing yaml must not use it. Falls back to `fallback` when no runtime is ready.
 * Call it from a test or hook, not at module level; the result is memoised per package.
 */
export function harnessPythonOr(packageDir: string, fallback: string): string {
	const cached = memo.get(packageDir);
	if (cached) {
		return cached;
	}
	const resolution = runtimeIsMaterialised(repoRootForPackage(packageDir))
		? resolveHarnessPython(packageDir)
		: undefined;
	const python = resolution?.ok && resolution.contract ? resolution.contract.python_executable : fallback;
	memo.set(packageDir, python);
	return python;
}
