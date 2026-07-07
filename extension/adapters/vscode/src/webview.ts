// Shared webview plumbing for BOTH placements (sidebar view + editor panel).
// They render the same React bundle (out/ui/main.js); a `mount` flag tells the
// app which layout to use. The webview never touches the network: it posts RPC
// messages to here, and this dispatches them to the AcmClient (HTTP -> gateway).

import * as vscode from 'vscode';
import * as fs from 'fs';
import { AcmClient } from './acmClient';

// Cache-buster for webview resources: VSCode caches the bundle by URL, so without
// a version that changes on rebuild a reloaded webview can serve a stale main.js
// (old tabs lingering after a recompile). The UI bundle's mtime changes on every
// build, so it's a perfect version token.
function bundleVersion(extUri: vscode.Uri): string {
  try {
    const p = vscode.Uri.joinPath(extUri, 'out', 'ui', 'main.js').fsPath;
    return String(Math.floor(fs.statSync(p).mtimeMs));
  } catch {
    return '0';
  }
}

// The current project root (workspace folder), injected into webviews as
// `window.acmProject` so the Chats list scopes to this project's chats — set
// once on activation by the extension host.
let _projectRoot = '';
export function setProjectRoot(root: string): void {
  _projectRoot = root || '';
}

// The gateway base URL, injected as `window.acmGateway` so the onboarding flow
// can show users exactly what their IDE/agent points at.
let _gatewayUrl = '';
export function setGatewayUrl(url: string): void {
  _gatewayUrl = url || '';
}

function nonce(): string {
  let s = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    s += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return s;
}

function renderHtml(
  webview: vscode.Webview,
  extUri: vscode.Uri,
  mount: string,
  conv?: string,
): string {
  const n = nonce();
  const v = bundleVersion(extUri);
  const js =
    webview.asWebviewUri(vscode.Uri.joinPath(extUri, 'out', 'ui', 'main.js')).toString() + '?v=' + v;
  const css =
    webview.asWebviewUri(vscode.Uri.joinPath(extUri, 'out', 'ui', 'main.css')).toString() + '?v=' + v;
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
  <script nonce="${n}">
    window.acmMount = ${JSON.stringify(mount)};
    window.acmProject = ${JSON.stringify(_projectRoot)};
    window.acmChat = ${JSON.stringify(conv || '')};
    window.acmGateway = ${JSON.stringify(_gatewayUrl)};
  </script>
  <script nonce="${n}" src="${js}"></script>
</body>
</html>`;
}

async function dispatch(client: AcmClient, method: string, params: any): Promise<unknown> {
  const p = params || {};
  switch (method) {
    case 'status':
      return client.status();
    case 'savings':
      return client.savings();
    case 'savingsReset':
      return client.savingsReset(p.conv || '');
    case 'preview':
      return client.preview(p.conv || '');
    case 'undoStatus':
      return client.undoStatus(p.conv || '');
    case 'undo':
      return client.undo(p.conv || '');
    case 'trainingSummary':
      return client.trainingSummary(!!p.includeModelLabels);
    case 'trainingExport':
      return client.trainingExport(!!p.includeModelLabels, p.dir || '');
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
    case 'contextWindows':
      return client.contextWindows(p.project || '');
    case 'getContextWindow':
      return client.getContextWindow(p.conv);
    case 'setWindowProfile':
      return client.setWindowProfile(p.conv, { name: p.name, body: p.body, clear: p.clear });
    case 'deleteWindow':
      return client.deleteWindow(p.conv);
    case 'resetWindows':
      return client.resetWindows();
    case 'messages':
      return client.messages(p.conv || '', !!p.full);
    case 'contextWindow':
      return client.contextWindow(p.conv || '');
    case 'contextTimeline':
      return client.contextTimeline(p.conv || '', p.limit || 50);
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

// Every live webview (sidebar, settings panel, chat panels, context-window
// panel) registers here so the realtime relay can push gateway events to all of
// them at once. A webview removes itself on dispose via the returned disposer.
const liveWebviews = new Set<vscode.Webview>();

/**
 * Open one SSE connection to the gateway and relay each event to every live
 * webview as an `acm-event` message. The React UIs listen for these and
 * refresh the affected view immediately — no polling, no Refresh button.
 * Returns a Disposable that closes the stream.
 */
export function startEventRelay(clientFactory: () => AcmClient): vscode.Disposable {
  const close = clientFactory().events((event) => {
    for (const wv of liveWebviews) {
      void wv.postMessage({ type: 'acm-event', event });
    }
  });
  return { dispose: close };
}

function wireRpc(
  webview: vscode.Webview,
  clientFactory: () => AcmClient,
  extUri: vscode.Uri,
): vscode.Disposable {
  liveWebviews.add(webview);
  const disposable = webview.onDidReceiveMessage(async (msg: any) => {
    if (!msg) {
      return;
    }
    // A chat row was clicked — open its two-column detail in an editor tab.
    if (msg.type === 'open-chat' && msg.conv) {
      openChatPanel(extUri, clientFactory, String(msg.conv));
      return;
    }
    if (msg.type !== 'rpc') {
      return;
    }
    try {
      // Webviews can't use window.confirm(); route confirmation through a
      // native VS Code modal instead.
      if (msg.method === 'confirm') {
        const pick = await vscode.window.showWarningMessage(
          String(msg.params?.message ?? 'Are you sure?'),
          { modal: true },
          'Yes',
        );
        void webview.postMessage({ type: 'rpc-result', id: msg.id, ok: true, data: pick === 'Yes' });
        return;
      }
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
  return {
    dispose: () => {
      liveWebviews.delete(webview);
      disposable.dispose();
    },
  };
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
    wireRpc(view.webview, this.clientFactory, this.extUri);
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
  wireRpc(panel.webview, clientFactory, extUri);
  panel.onDidDispose(() => (panel = undefined));
}

/** One chat's two-column detail (context window | per-chat settings), opened as
 *  an editor tab from the Chats list. One panel reused per chat id. */
const chatPanels = new Map<string, vscode.WebviewPanel>();

export function openChatPanel(
  extUri: vscode.Uri,
  clientFactory: () => AcmClient,
  conv: string,
): void {
  const existing = chatPanels.get(conv);
  if (existing) {
    existing.reveal(vscode.ViewColumn.Active);
    return;
  }
  const p = vscode.window.createWebviewPanel(
    'acmChat',
    'ACM Chat',
    vscode.ViewColumn.Active,
    { enableScripts: true, localResourceRoots: [extUri], retainContextWhenHidden: true },
  );
  p.webview.html = renderHtml(p.webview, extUri, 'chat', conv);
  wireRpc(p.webview, clientFactory, extUri);
  chatPanels.set(conv, p);
  p.onDidDispose(() => chatPanels.delete(conv));
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
  wireRpc(cwPanel.webview, clientFactory, extUri);
  cwPanel.onDidDispose(() => (cwPanel = undefined));
}
