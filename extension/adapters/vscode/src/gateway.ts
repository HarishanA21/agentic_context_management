// Owns the acm-gateway process lifecycle so Claude Code can be safely pointed at
// it. Starts the gateway on activation, supervises it (restart-on-crash), and
// stops it on deactivation. If a gateway is already serving the URL (the user
// started one by hand), we *adopt* it — never spawning a duplicate and never
// killing their process. This is what makes user-settings routing safe: while
// the extension is active, the gateway it points Claude Code at is guaranteed up.

import * as http from 'http';
import { spawn, ChildProcess } from 'child_process';
import { URL } from 'url';
import * as vscode from 'vscode';

export type GatewayState = 'stopped' | 'starting' | 'running' | 'adopted' | 'failed';

export interface GatewayOptions {
  /** Shell command that launches the gateway (e.g. `acm-gateway`). */
  command: string;
  /** Base URL the gateway listens on (e.g. http://127.0.0.1:8807). */
  url: string;
  output: vscode.OutputChannel;
  /** Environment for spawning the gateway (e.g. an augmented PATH). Defaults to process.env. */
  env?: NodeJS.ProcessEnv;
}

const MAX_RESTARTS = 3;

export class GatewayManager {
  private child: ChildProcess | undefined;
  private ownsProcess = false;
  private stopping = false;
  private restarts = 0;
  private _state: GatewayState = 'stopped';
  private readonly _onState = new vscode.EventEmitter<GatewayState>();
  /** Fires on every state transition — the HUD listens to this. */
  readonly onState = this._onState.event;

  constructor(private readonly opts: GatewayOptions) {}

  get state(): GatewayState {
    return this._state;
  }

  /** True once the gateway is up — whether we spawned it or adopted one. */
  get isUp(): boolean {
    return this._state === 'running' || this._state === 'adopted';
  }

  private set(state: GatewayState): void {
    this._state = state;
    this._onState.fire(state);
  }

  private log(msg: string): void {
    this.opts.output.appendLine(`[gateway] ${msg}`);
  }

  /** Adopt an already-running gateway, else spawn one and wait for health. */
  async start(): Promise<GatewayState> {
    if (await pingStatus(this.opts.url, 1500)) {
      this.ownsProcess = false;
      this.log(`adopted existing gateway at ${this.opts.url}`);
      this.set('adopted');
      return this._state;
    }
    return this.spawnAndWait();
  }

  private async spawnAndWait(): Promise<GatewayState> {
    this.set('starting');
    this.stopping = false;
    this.log(`starting: ${this.opts.command}`);
    try {
      // shell:true so a bare command, a venv path, or `python -m acm_gateway`
      // all work. Inherit our env so ACM_* (incl. ACM_ANTHROPIC_AUTH_MODE) flow.
      this.child = spawn(this.opts.command, { shell: true, env: this.opts.env ?? process.env });
    } catch (e) {
      this.log(`spawn failed: ${(e as Error).message}`);
      this.set('failed');
      return this._state;
    }
    this.ownsProcess = true;
    this.child.stdout?.on('data', (d) => this.opts.output.append(String(d)));
    this.child.stderr?.on('data', (d) => this.opts.output.append(String(d)));
    this.child.on('exit', (code, signal) => this.onExit(code, signal));

    const ok = await waitHealthy(this.opts.url, 20, 500);
    if (ok) {
      this.restarts = 0;
      this.log('gateway healthy');
      this.set('running');
    } else {
      this.log('gateway did not become healthy in time');
      this.set('failed');
    }
    return this._state;
  }

  private onExit(code: number | null, signal: string | null): void {
    this.child = undefined;
    if (this.stopping) {
      this.set('stopped');
      return;
    }
    this.log(`gateway exited unexpectedly (code=${code}, signal=${signal})`);
    if (this.ownsProcess && this.restarts < MAX_RESTARTS) {
      this.restarts++;
      this.log(`restarting (attempt ${this.restarts}/${MAX_RESTARTS})`);
      void this.spawnAndWait();
    } else {
      this.set('failed');
    }
  }

  async restart(): Promise<GatewayState> {
    await this.stop();
    this.restarts = 0;
    return this.start();
  }

  /** Stop a gateway we spawned. Adopted (user-started) gateways are left alone. */
  async stop(): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (child && this.ownsProcess && child.pid !== undefined) {
      this.log('stopping gateway');
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => {
          try {
            child.kill('SIGKILL');
          } catch {
            /* already gone */
          }
          resolve();
        }, 2000);
        child.once('exit', () => {
          clearTimeout(timer);
          resolve();
        });
        child.kill('SIGTERM');
      });
    }
    this.child = undefined;
    this.set('stopped');
  }

  dispose(): void {
    void this.stop();
    this._onState.dispose();
  }
}

/** GET {url}/status — resolves true on any non-error HTTP response. */
function pingStatus(url: string, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    let u: URL;
    try {
      u = new URL('/status', url);
    } catch {
      resolve(false);
      return;
    }
    const req = http.get(
      { hostname: u.hostname, port: u.port, path: u.pathname, timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 400);
      },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

export function healthy(url: string): Promise<boolean> {
  return pingStatus(url, 1500);
}

async function waitHealthy(url: string, tries: number, delayMs: number): Promise<boolean> {
  for (let i = 0; i < tries; i++) {
    if (await pingStatus(url, 1000)) {
      return true;
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return false;
}
