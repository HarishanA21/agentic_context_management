// ACM VSCode extension entry point. Wires in:
//   1. The settings UI (React webview) in BOTH placements — a sidebar view and
//      an editor-panel tab — sharing one bundle (see webview.ts).
//   2. Language Model Tools (registerTools) — callable by Copilot agent mode.
//   3. Commands + a simple @acm chat participant.
//   4. The gateway lifecycle: the extension starts/supervises acm-gateway and
//      points Claude Code at it (env.ANTHROPIC_BASE_URL), so subscription turns
//      are monitored with no manual setup — and routing is removed on shutdown
//      so Claude Code never points at a gateway that isn't running.
// Everything talks to the local acm-gateway over HTTP (see acmClient.ts).

import * as vscode from 'vscode';
import { AcmClient } from './acmClient';
import { registerTools } from './tools';
import {
  AcmViewProvider,
  openSettingsPanel,
  openContextWindowPanel,
  setProjectRoot,
} from './webview';
import { GatewayManager, healthy } from './gateway';
import { enableRouting, disableRoutingAt, RouteScope } from './claudeRouting';

interface RoutedOwnership {
  path: string;
  url: string;
  scope: RouteScope;
}

const ROUTING_KEY = 'acm.routing';

let _gateway: GatewayManager | undefined;
let _output: vscode.OutputChannel | undefined;
let _context: vscode.ExtensionContext | undefined;

function acmCfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration('acm');
}

function gatewayUrl(): string {
  return acmCfg().get<string>('gatewayUrl', 'http://127.0.0.1:8807');
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function client(): AcmClient {
  return new AcmClient(gatewayUrl());
}

// ── routing: point Claude Code at the gateway (or stop) ───────────────────

/** Write env.ANTHROPIC_BASE_URL once the gateway is confirmed up. `manual` =
 *  invoked from a command (bypasses the routeClaudeCode setting + shows toasts). */
async function applyRouting(manual: boolean): Promise<void> {
  const cfg = acmCfg();
  if (!manual && !cfg.get<boolean>('routeClaudeCode', true)) {
    return;
  }
  const url = gatewayUrl();
  const scope = cfg.get<RouteScope>('routeScope', 'user');
  const root = workspaceRoot();

  // Never point Claude Code at a gateway that isn't actually answering.
  if (!(await healthy(url))) {
    _output?.appendLine('[routing] gateway not reachable — skipping ANTHROPIC_BASE_URL write');
    if (manual) {
      vscode.window.showWarningMessage('ACM: gateway not reachable — not routing Claude Code yet.');
    }
    return;
  }

  let res = enableRouting(url, scope, root);
  if (res.conflict) {
    const pick = await vscode.window.showWarningMessage(
      `Claude Code already sets ANTHROPIC_BASE_URL=${res.conflict} in ${res.path}. ` +
        'Route it through the ACM gateway instead?',
      'Use ACM gateway',
      'Keep mine',
    );
    if (pick !== 'Use ACM gateway') {
      return;
    }
    res = enableRouting(url, scope, root, true);
  }
  if (res.error) {
    vscode.window.showErrorMessage(`ACM routing: ${res.error}`);
    return;
  }
  if (res.ok && res.path) {
    if (res.changed) {
      await _context?.globalState.update(ROUTING_KEY, { path: res.path, url, scope } as RoutedOwnership);
      _output?.appendLine(`[routing] ANTHROPIC_BASE_URL=${url} -> ${res.path}`);
      vscode.window.showInformationMessage(
        'ACM is now monitoring Claude Code (subscription-safe — no API key needed). ' +
          'Restart Claude Code for it to take effect.',
      );
    } else if (manual) {
      vscode.window.showInformationMessage('ACM: Claude Code is already routed through the gateway.');
    }
  }
}

/** Remove the env we wrote (from the exact file we recorded). */
async function clearRouting(manual: boolean): Promise<void> {
  const owned = _context?.globalState.get<RoutedOwnership>(ROUTING_KEY);
  if (!owned) {
    if (manual) {
      vscode.window.showInformationMessage('ACM: Claude Code routing is not managed by the extension.');
    }
    return;
  }
  const res = disableRoutingAt(owned.path, owned.url);
  await _context?.globalState.update(ROUTING_KEY, undefined);
  _output?.appendLine(`[routing] removed ANTHROPIC_BASE_URL from ${owned.path}`);
  if (res.error) {
    _output?.appendLine(`[routing] (note) ${res.error}`);
  }
  if (manual) {
    vscode.window.showInformationMessage('ACM: stopped routing Claude Code. Restart Claude Code to apply.');
  }
}

/** Start (or adopt) the gateway, then route Claude Code at it. Runs in the
 *  background so activation stays fast. */
async function setupLifecycle(context: vscode.ExtensionContext): Promise<void> {
  const cfg = acmCfg();
  if (cfg.get<boolean>('manageGateway', true)) {
    _gateway = new GatewayManager({
      command: cfg.get<string>('gatewayCommand', 'acm-gateway'),
      url: gatewayUrl(),
      output: _output!,
    });
    context.subscriptions.push({ dispose: () => _gateway?.dispose() });
    _gateway.onState((s) => _output?.appendLine(`[gateway] state -> ${s}`));
    const state = await _gateway.start();
    if (state === 'failed') {
      vscode.window.showWarningMessage(
        'ACM: could not start the gateway. Set `acm.gatewayCommand` (it must be on PATH, ' +
          'e.g. `uv tool install acm-context-management`), or run it yourself.',
      );
    }
  }
  await applyRouting(false);
}

export function activate(context: vscode.ExtensionContext): void {
  _context = context;
  _output = vscode.window.createOutputChannel('ACM Gateway');
  context.subscriptions.push(_output);

  // Scope the Chats list to this workspace's project (Claude-Code-style).
  setProjectRoot(workspaceRoot() || '');

  registerTools(context, client);

  // Sidebar placement (Activity Bar -> "ACM" -> Context Management view).
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      'acm.sidebar',
      new AcmViewProvider(context.extensionUri, client),
    ),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('acm.openSettings', () =>
      openSettingsPanel(context.extensionUri, client),
    ),
    vscode.commands.registerCommand('acm.showContextWindow', () =>
      openContextWindowPanel(context.extensionUri, client),
    ),
    vscode.commands.registerCommand('acm.showStatus', async () => {
      try {
        const s = await client().status();
        const on = Object.entries(s.techniques)
          .filter(([, v]) => v && v !== 'off')
          .map(([k]) => k);
        const auth = s.auth?.subscription
          ? 'monitoring subscription'
          : s.auth
            ? `auth: ${s.auth.mode}`
            : '';
        vscode.window.showInformationMessage(
          `ACM gateway OK · ${auth ? auth + ' · ' : ''}active: ${on.join(', ') || 'none'}`,
        );
      } catch (e) {
        vscode.window.showErrorMessage(`ACM gateway unreachable: ${(e as Error).message}`);
      }
    }),
    vscode.commands.registerCommand('acm.enableMonitoring', () => applyRouting(true)),
    vscode.commands.registerCommand('acm.disableMonitoring', () => clearRouting(true)),
    vscode.commands.registerCommand('acm.restartGateway', async () => {
      if (!_gateway) {
        vscode.window.showInformationMessage(
          'ACM: the gateway is not managed by the extension (acm.manageGateway is off).',
        );
        return;
      }
      const state = await _gateway.restart();
      vscode.window.showInformationMessage(`ACM gateway: ${state}`);
    }),
    vscode.commands.registerCommand('acm.recall', async () => {
      const query = await vscode.window.showInputBox({ prompt: 'ACM recall — filter (blank = all)' });
      if (query === undefined) {
        return;
      }
      const scope = acmCfg().get<string>('memoryScope', 'user');
      const res = await client().recall(query, scope);
      vscode.window.showInformationMessage(res.items.length ? res.items.join(' · ') : '(no memories)');
    }),
  );

  // Status-bar HUD — live context-token estimate + subscription indicator for
  // the latest conversation the gateway has seen. Click opens the status detail.
  const hud = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  hud.command = 'acm.showStatus';
  context.subscriptions.push(hud);

  async function refreshHud(): Promise<void> {
    try {
      const s = await client().status();
      const tok = Number(s?.context?.tokens || 0);
      const fmt = tok >= 1000 ? (tok / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(tok);
      const sub = s.auth?.subscription;
      hud.text = `$(history) ACM ${fmt}${sub ? ' $(broadcast)' : ''}`;
      hud.tooltip =
        `ACM context: ~${tok.toLocaleString()} tokens (latest conversation)` +
        (sub
          ? ' · monitoring your Claude subscription'
          : s.auth
            ? ` · auth: ${s.auth.mode}`
            : '') +
        ' · click for status';
    } catch {
      hud.text = '$(history) ACM offline';
      hud.tooltip = 'ACM gateway unreachable — start it to manage context';
    }
    hud.show();
  }
  void refreshHud();
  const hudTimer = setInterval(() => void refreshHud(), 15000);
  context.subscriptions.push({ dispose: () => clearInterval(hudTimer) });

  registerChatParticipant(context);

  // Start/adopt the gateway and route Claude Code at it — in the background so
  // activation returns immediately.
  void setupLifecycle(context);
}

function registerChatParticipant(context: vscode.ExtensionContext): void {
  const handler: vscode.ChatRequestHandler = async (request, _ctx, stream) => {
    const c = client();
    const prompt = request.prompt.trim();
    try {
      if (request.command === 'status' || /^status\b/i.test(prompt)) {
        const s = await c.status();
        stream.markdown(`**ACM gateway** — upstream \`${s.upstream}\`\n\n`);
        if (s.auth) {
          stream.markdown(
            `- auth: \`${s.auth.mode}\`${s.auth.subscription ? ' (monitoring subscription)' : ''}\n`,
          );
        }
        for (const [k, v] of Object.entries(s.techniques)) {
          stream.markdown(`- ${k}: \`${String(v)}\`\n`);
        }
        return;
      }
      if (/^recall\b/i.test(prompt)) {
        const q = prompt.replace(/^recall\b/i, '').trim();
        const res = await c.recall(q);
        stream.markdown(res.items.length ? res.items.map((i) => `- ${i}`).join('\n') : '_(no memories)_');
        return;
      }
      stream.markdown(
        'ACM helper. Try `@acm status`, `@acm recall <query>`, or open the ' +
          'sidebar / `ACM: Open Context-Management Settings` for the full UI.',
      );
    } catch (e) {
      stream.markdown(`⚠️ Can't reach the gateway: ${(e as Error).message}`);
    }
  };

  const participant = vscode.chat.createChatParticipant('acm.agent', handler);
  participant.iconPath = new vscode.ThemeIcon('layers');
  context.subscriptions.push(participant);
}

export async function deactivate(): Promise<void> {
  // Stop routing Claude Code *before* the gateway goes down, so it's never left
  // pointing at a dead endpoint.
  try {
    await clearRouting(false);
  } catch {
    /* best-effort */
  }
  if (_gateway) {
    await _gateway.stop();
  }
}
