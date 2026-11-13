#!/usr/bin/env node
/**
 * The interpreter every repository check uses for its Python probes.
 *
 * A bare `python3` is not portable: Windows installs usually expose only
 * `python`, and the host interpreter does not carry the checks' dependencies
 * (pyyaml). Prefer the harness interpreter a caller already exported; otherwise
 * run through uv with a pinned 3.11 and pyyaml, both of which the README
 * requires. `--no-project` keeps uv out of whatever project it is invoked from
 * (the public-export tree has no root project of its own).
 */

export function pythonInvocation(args, { harnessOnly = false } = {}) {
	const harness = process.env.HARNESS_PYTHON_BIN || process.env.SURE_EVAL_HARNESS_PYTHON_BIN;
	if (harness) {
		return [harness, args];
	}
	if (harnessOnly) {
		return null;
	}
	return ["uv", ["run", "--no-project", "--python", "3.11", "--with", "pyyaml", "python", ...args]];
}
