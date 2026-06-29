// ACM VSCode extension entry point. Wires in:
//   1. The settings UI (React webview) in BOTH placements — a sidebar view and
//      an editor-panel tab — sharing one bundle (see webview.ts).
//   2. Language Model Tools (registerTools) — callable by Copilot agent mode.
//   3. Commands + a simple @acm chat participant.
// Everything talks to the local acm-gateway over HTTP (see acmClient.ts).

import * as vscode from 'vscode';
import { AcmClient } from './acmClient';
import { registerTools } from './tools';
import { AcmViewProvider, openSettingsPanel, openContextWindowPanel } from './webview';

function gatewayUrl(): string {
  return vscode.workspace.getConfiguration('acm').get<string>('gatewayUrl', 'http://127.0.0.1:8807');
}

function client(): AcmClient {
  return new AcmClient(gatewayUrl());
}

export function activate(context: vscode.ExtensionContext): void {
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
        vscode.window.showInformationMessage(
          `ACM gateway OK · upstream ${s.upstream} · active: ${on.join(', ') || 'none'}`,
        );
      } catch (e) {
        vscode.window.showErrorMessage(`ACM gateway unreachable: ${(e as Error).message}`);
      }
    }),
    vscode.commands.registerCommand('acm.recall', async () => {
      const query = await vscode.window.showInputBox({ prompt: 'ACM recall — filter (blank = all)' });
      if (query === undefined) {
        return;
      }
      const scope = vscode.workspace.getConfiguration('acm').get<string>('memoryScope', 'user');
      const res = await client().recall(query, scope);
      vscode.window.showInformationMessage(res.items.length ? res.items.join(' · ') : '(no memories)');
    }),
  );

  // Status-bar HUD — live context-token estimate for the latest conversation
  // the gateway has seen. Click opens the status detail.
  const hud = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  hud.command = 'acm.showStatus';
  context.subscriptions.push(hud);

  async function refreshHud(): Promise<void> {
    try {
      const s: any = await client().status();
      const tok = Number(s?.context?.tokens || 0);
      const fmt = tok >= 1000 ? (tok / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(tok);
      hud.text = `$(history) ACM ${fmt}`;
      hud.tooltip = `ACM context: ~${tok.toLocaleString()} tokens (latest conversation) · click for status`;
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
}

function registerChatParticipant(context: vscode.ExtensionContext): void {
  const handler: vscode.ChatRequestHandler = async (request, _ctx, stream) => {
    const c = client();
    const prompt = request.prompt.trim();
    try {
      if (request.command === 'status' || /^status\b/i.test(prompt)) {
        const s = await c.status();
        stream.markdown(`**ACM gateway** — upstream \`${s.upstream}\`\n\n`);
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

export function deactivate(): void {
  /* no-op */
}
