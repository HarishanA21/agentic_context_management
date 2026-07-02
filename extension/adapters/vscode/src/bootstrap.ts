// Makes the acm-gateway available without the user running anything by hand.
//
// The gateway is a Python service shipped as the `acm-context-management` wheel.
// A .vsix can't run Python, so on first activation we ensure the tool exists:
//   1. If `acm-gateway` is already resolvable, use it.
//   2. Otherwise install it with `uv` (bootstrapping `uv` itself if missing).
//   3. Fall back to `uv tool run` (uvx) if the installed binary can't be located.
//
// GUI apps on macOS/Windows don't inherit the shell PATH, so we both augment the
// PATH we search and hand that augmented env to the GatewayManager for spawning.

import * as vscode from 'vscode';
import { spawn } from 'child_process';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

const PKG = 'acm-context-management';
const DEFAULT_COMMAND = 'acm-gateway';

export interface ResolvedGateway {
  /** Command line for the GatewayManager to spawn (shell:true). */
  command: string;
  /** Environment with an augmented PATH so the command resolves. */
  env: NodeJS.ProcessEnv;
}

/** Extra bin directories where `uv` and uv-installed tools commonly land. */
function extraBinDirs(): string[] {
  const home = os.homedir();
  const dirs = [
    path.join(home, '.local', 'bin'),
    path.join(home, '.cargo', 'bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
  ];
  if (process.platform === 'win32') {
    dirs.push(
      path.join(home, '.local', 'bin'),
      path.join(home, 'AppData', 'Roaming', 'uv', 'tools'),
      path.join(home, '.cargo', 'bin'),
    );
  }
  return dirs.filter((d) => {
    try {
      return fs.existsSync(d);
    } catch {
      return false;
    }
  });
}

/** A copy of process.env with our extra bin dirs prepended to PATH. */
function augmentedEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  const sep = path.delimiter;
  const existing = env.PATH || env.Path || '';
  const merged = [...extraBinDirs(), existing].filter(Boolean).join(sep);
  env.PATH = merged;
  if ('Path' in env) env.Path = merged;
  return env;
}

interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

/** Run a command to completion, capturing output. Never rejects. */
function run(command: string, env: NodeJS.ProcessEnv, timeoutMs = 180_000): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(command, { shell: true, env });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL');
      } catch {
        /* already gone */
      }
    }, timeoutMs);
    child.stdout?.on('data', (d) => (stdout += String(d)));
    child.stderr?.on('data', (d) => (stderr += String(d)));
    child.on('error', (e) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: stderr + String(e) });
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

/** Absolute path to `bin` if it resolves on the (augmented) PATH, else null. */
async function locate(bin: string, env: NodeJS.ProcessEnv): Promise<string | null> {
  const probe = process.platform === 'win32' ? `where ${bin}` : `command -v ${bin}`;
  const res = await run(probe, env, 10_000);
  if (res.code === 0) {
    const first = res.stdout.split(/\r?\n/).map((l) => l.trim()).find(Boolean);
    if (first) return first;
  }
  return null;
}

function log(output: vscode.OutputChannel, msg: string): void {
  output.appendLine(`[bootstrap] ${msg}`);
}

/** Install uv via the official installer for this platform. */
async function installUv(env: NodeJS.ProcessEnv, output: vscode.OutputChannel): Promise<string | null> {
  log(output, 'uv not found — installing uv');
  const cmd =
    process.platform === 'win32'
      ? 'powershell -ExecutionPolicy ByPass -NoProfile -c "irm https://astral.sh/uv/install.ps1 | iex"'
      : 'curl -LsSf https://astral.sh/uv/install.sh | sh';
  const res = await run(cmd, env);
  if (res.code !== 0) {
    log(output, `uv install failed (code ${res.code}): ${res.stderr.trim()}`);
    return null;
  }
  // The installer may have created ~/.local/bin just now; re-evaluate PATH.
  const fresh = augmentedEnv();
  env.PATH = fresh.PATH;
  if ('Path' in env) env.Path = fresh.Path;
  return locate('uv', env);
}

/**
 * Ensure the gateway can be launched. Returns the command + env to spawn it with,
 * or null if it could not be made available (caller falls back to a hint message).
 */
export async function ensureGateway(
  configuredCommand: string,
  output: vscode.OutputChannel,
): Promise<ResolvedGateway | null> {
  const env = augmentedEnv();

  // The user pointed us at a custom command — trust it, just augment PATH.
  if (configuredCommand && configuredCommand !== DEFAULT_COMMAND) {
    log(output, `using configured gatewayCommand: ${configuredCommand}`);
    return { command: configuredCommand, env };
  }

  // Already installed and resolvable.
  const existing = await locate(DEFAULT_COMMAND, env);
  if (existing) {
    log(output, `found ${DEFAULT_COMMAND} at ${existing}`);
    return { command: quote(existing), env };
  }

  return vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: 'ACM: setting up the context-management gateway…',
      cancellable: false,
    },
    async (progress): Promise<ResolvedGateway | null> => {
      let uv = await locate('uv', env);
      if (!uv) {
        progress.report({ message: 'installing uv…' });
        uv = await installUv(env, output);
      }
      if (!uv) {
        log(output, 'could not obtain uv; gateway cannot be auto-installed');
        return null;
      }
      log(output, `using uv at ${uv}`);

      progress.report({ message: `installing ${PKG}…` });
      const install = await run(`${quote(uv)} tool install ${PKG}`, env);
      if (install.code !== 0) {
        log(output, `uv tool install failed (code ${install.code}): ${install.stderr.trim()}`);
      } else {
        log(output, `${PKG} installed`);
      }

      // Prefer the installed binary; refresh PATH first (uv tool installs to ~/.local/bin).
      const fresh = augmentedEnv();
      env.PATH = fresh.PATH;
      if ('Path' in env) env.Path = fresh.Path;

      const installed = await locate(DEFAULT_COMMAND, env);
      if (installed) {
        log(output, `gateway ready at ${installed}`);
        return { command: quote(installed), env };
      }

      // Fall back to running it through uv without a stable binary on PATH.
      log(output, 'binary not on PATH — falling back to `uv tool run`');
      return { command: `${quote(uv)} tool run --from ${PKG} ${DEFAULT_COMMAND}`, env };
    },
  );
}

/** Quote a path that may contain spaces for a shell:true spawn. */
function quote(p: string): string {
  if (!/\s/.test(p)) return p;
  return process.platform === 'win32' ? `"${p}"` : `'${p.replace(/'/g, `'\\''`)}'`;
}
