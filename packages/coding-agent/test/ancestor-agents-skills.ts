import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { canonicalizePath, resolvePath } from "../src/utils/paths.ts";

/**
 * The nearest ancestor of `dir` (inclusive) that holds a .agents/skills directory which
 * hasTrustRequiringProjectResources() would not ignore as the user's own. Mirrors that function:
 * both $HOME and os.homedir() count as the user's home, and paths are canonicalised. A scrubbed
 * test environment points HOME at a throwaway directory, but the OS temp directory can still sit
 * inside the real profile (always on Windows), so that profile's ~/.agents/skills becomes a
 * project ancestor of every temp project.
 */
export function foreignAgentsSkillsAncestor(dir: string): string | undefined {
	const userSkillsDirs = new Set(
		[process.env.HOME, homedir()]
			.filter((home): home is string => !!home)
			.map((home) => join(canonicalizePath(resolvePath(home)), ".agents", "skills")),
	);
	for (let current = canonicalizePath(resolvePath(dir)); ; current = dirname(current)) {
		const skills = join(current, ".agents", "skills");
		if (!userSkillsDirs.has(skills) && existsSync(skills)) {
			return current;
		}
		if (dirname(current) === current) {
			return undefined;
		}
	}
}
