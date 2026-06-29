// Shared webview plumbing for BOTH placements (sidebar view + editor panel).
// They render the same React bundle (out/ui/main.js); a `mount` flag tells the
// app which layout to use. The webview never touches the network: it posts RPC
// messages to here, and this dispatches them to the AcmClient (HTTP -> gateway).

import * as vscode from 'vscode';
import { AcmClient } from './acmClient';

function nonce(): string {
  let s = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    s += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return s;
}

function renderHtml(webview: vscode.Webview, extUri: vscode.Uri, mount: string): string {
  const n = nonce();
  const js = webview.asWebviewUri(vscode.Uri.joinPath(extUri, 'out', 'ui', 'main.js'));
  const css = webview.asWebviewUri(vscode.Uri.joinPath(extUri, 'out', 'ui', 'main.css'));
  const csp =
    `default-src 'none'; img-src ${webview.cspSource} data:; ` +
    `font-src ${webview.cspSource}; style-src ${webview.cspSource} 'unsafe-inline'; ` +
    `script-src 'nonce-${n}';`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link href="${css}" rel="stylesheet" />
  <title>ACM</title>
</head>
<body>
  <div id="root"></div>
  <script nonce="${n}">window.acmMount = ${JSON.stringify(mount)};</script>
  <script nonce="${n}" src="${js}"></script>
</body>
</html>`;
}

async function dispatch(client: AcmClient, method: string, params: any): Promise<unknown> {
  const p = params || {};
  switch (method) {
    case 'status':
      return client.status();
    case 'getProfile':
      return client.getProfile();
    case 'setPreset':
      return client.setProfile(p.name);
    case 'setProfileBody':
      return client.setProfileBody(p.body, p.visual_method);
    case 'providers':
      return client.providers();
    case 'setDefaultProvider':
      return client.setDefaultProvider(p.slug);
    case 'upsertProvider':
      return client.upsertProvider(p.cfg);
    case 'deleteProvider':
      return client.deleteProvider(p.slug);
    case 'recall':
      return client.recall(p.query || '', p.scope || 'user');
    case 'remember':
      return client.remember(p.text, p.scope || 'user');
    case 'memoryClear':
      return client.memoryClear(p.scope || 'user');
    case 'conversations':
      return client.conversations();
    case 'messages':
      return client.messages(p.conv || '');
    case 'contextWindow':
      return client.contextWindow(p.conv || '');
    case 'dropMessage':
      return client.dropMessage(p.fp, p.conv || '');
    case 'restoreMessage':
      return client.restoreMessage(p.fp, p.conv || '');
    case 'dropMany':
      return client.dropMany(p.fps || [], p.conv || '');
    case 'relevanceSuggest':
      return client.relevanceSuggest(p.conv || '');
    case 'relevanceFeedback':
      return client.relevanceFeedback(p.payload || {});
    case 'relevanceSummarize':
      return client.relevanceSummarize({
        member_fps: p.member_fps || p.fps || [],
        conv: p.conv || '',
        title: p.title,
        model: p.model,
      });
    case 'messageImages':
      return client.messageImages(p.fp, p.conv || '');
    case 'messageText':
      return client.messageText(p.fp, p.conv || '');
    default:
      throw new Error('unknown rpc method: ' + method);
  }
}

function wireRpc(webview: vscode.Webview, clientFactory: () => AcmClient): vscode.Disposable {
  return webview.onDidReceiveMessage(async (msg: any) => {
    if (!msg || msg.type !== 'rpc') {
      return;
    }
    try {
      const data = await dispatch(clientFactory(), msg.method, msg.params);
      void webview.postMessage({ type: 'rpc-result', id: msg.id, ok: true, data });
    } catch (e) {
      void webview.postMessage({
        type: 'rpc-result',
        id: msg.id,
        ok: false,
        error: (e as Error).message,
      });
    }
  });
}

/** Sidebar placement (Activity Bar -> webview view). */
export class AcmViewProvider implements vscode.WebviewViewProvider {
  constructor(
    private readonly extUri: vscode.Uri,
    private readonly clientFactory: () => AcmClient,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    view.webview.options = { enableScripts: true, localResourceRoots: [this.extUri] };
    view.webview.html = renderHtml(view.webview, this.extUri, 'sidebar');
    wireRpc(view.webview, this.clientFactory);
  }
}

/** Editor-panel placement (opens as a tab). One panel reused across calls. */
let panel: vscode.WebviewPanel | undefined;

export function openSettingsPanel(extUri: vscode.Uri, clientFactory: () => AcmClient): void {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Active);
    return;
  }
  panel = vscode.window.createWebviewPanel(
    'acmSettings',
    'ACM Context Management',
    vscode.ViewColumn.Active,
    { enableScripts: true, localResourceRoots: [extUri], retainContextWhenHidden: true },
  );
  panel.webview.html = renderHtml(panel.webview, extUri, 'panel');
  wireRpc(panel.webview, clientFactory);
  panel.onDidDispose(() => (panel = undefined));
}

/** Standalone "Context Window" editor tab — the same React bundle mounted via
 *  the `context-window` flag so it renders just that view full-screen. */
let cwPanel: vscode.WebviewPanel | undefined;

export function openContextWindowPanel(
  extUri: vscode.Uri,
  clientFactory: () => AcmClient,
): void {
  if (cwPanel) {
    cwPanel.reveal(vscode.ViewColumn.Active);
    return;
  }
  cwPanel = vscode.window.createWebviewPanel(
    'acmContextWindow',
    'ACM Context Window',
    vscode.ViewColumn.Active,
    { enableScripts: true, localResourceRoots: [extUri], retainContextWhenHidden: true },
  );
  cwPanel.webview.html = renderHtml(cwPanel.webview, extUri, 'context-window');
  wireRpc(cwPanel.webview, clientFactory);
  cwPanel.onDidDispose(() => (cwPanel = undefined));
}
