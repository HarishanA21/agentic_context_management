// Points Claude Code at the gateway by managing `env.ANTHROPIC_BASE_URL` in a
// Claude Code settings file:
//   * user (default)  — ~/.claude/settings.json  (per-machine, all projects)
//   * project (advanced) — <workspace>/.claude/settings.local.json
//     (this workspace only, and uncommitted by Claude Code convention)
//
// Safety contract: we only ever set the key to *our* gateway URL, only write
// when the value actually changes, and on removal only delete a value that still
// equals our URL — so a user's own ANTHROPIC_BASE_URL is never clobbered, and
// Claude Code is never left pointing at a gateway the extension didn't place.

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

export type RouteScope = 'user' | 'project';

const KEY = 'ANTHROPIC_BASE_URL';

export interface RouteResult {
  ok: boolean;
  /** The settings file we targeted. */
  path?: string;
  /** True when we actually wrote the file (value was missing/different). */
  changed?: boolean;
  /** An existing, *different* ANTHROPIC_BASE_URL we refused to overwrite. */
  conflict?: string;
  error?: string;
}

/** Resolve the Claude Code settings file for a scope (undefined if a
 *  project scope has no workspace folder). */
export function claudeSettingsPath(
  scope: RouteScope,
  workspaceRoot?: string,
): string | undefined {
  if (scope === 'project') {
    return workspaceRoot
      ? path.join(workspaceRoot, '.claude', 'settings.local.json')
      : undefined;
  }
  return path.join(os.homedir(), '.claude', 'settings.json');
}

function readJson(file: string): { obj?: Record<string, unknown>; error?: string } {
  if (!fs.existsSync(file)) {
    return { obj: {} };
  }
  try {
    const txt = fs.readFileSync(file, 'utf8').trim();
    return { obj: txt ? (JSON.parse(txt) as Record<string, unknown>) : {} };
  } catch (e) {
    return { error: (e as Error).message };
  }
}

function writeJson(file: string, obj: Record<string, unknown>): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

/** Set env.ANTHROPIC_BASE_URL = url. Refuses (conflict) if a different value is
 *  already present unless `force`. */
export function enableRouting(
  url: string,
  scope: RouteScope,
  workspaceRoot?: string,
  force = false,
): RouteResult {
  const file = claudeSettingsPath(scope, workspaceRoot);
  if (!file) {
    return { ok: false, error: 'open a folder to use project-scoped routing' };
  }
  const { obj, error } = readJson(file);
  if (error || !obj) {
    return { ok: false, path: file, error: `settings file isn't valid JSON: ${error}` };
  }
  const env =
    obj.env && typeof obj.env === 'object'
      ? (obj.env as Record<string, unknown>)
      : ((obj.env = {}) as Record<string, unknown>);
  const existing = env[KEY];
  if (existing === url) {
    return { ok: true, path: file, changed: false };
  }
  if (existing && !force) {
    return { ok: false, path: file, conflict: String(existing) };
  }
  env[KEY] = url;
  writeJson(file, obj);
  return { ok: true, path: file, changed: true };
}

/** Remove env.ANTHROPIC_BASE_URL from a specific file, only if it still equals
 *  our URL (so we never delete a value the user later changed by hand). */
export function disableRoutingAt(file: string, url: string): RouteResult {
  if (!file || !fs.existsSync(file)) {
    return { ok: true, path: file };
  }
  const { obj, error } = readJson(file);
  if (error || !obj) {
    return { ok: false, path: file, error };
  }
  const env = obj.env as Record<string, unknown> | undefined;
  if (env && typeof env === 'object' && env[KEY] === url) {
    delete env[KEY];
    if (Object.keys(env).length === 0) {
      delete obj.env;
    }
    writeJson(file, obj);
    return { ok: true, path: file, changed: true };
  }
  return { ok: true, path: file, changed: false };
}

/** Remove routing for a scope (resolves the file, then delegates). */
export function disableRouting(
  url: string,
  scope: RouteScope,
  workspaceRoot?: string,
): RouteResult {
  const file = claudeSettingsPath(scope, workspaceRoot);
  if (!file) {
    return { ok: true };
  }
  return disableRoutingAt(file, url);
}
