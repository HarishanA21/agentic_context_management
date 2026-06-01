// Webview settings panel. Lists the built-in presets (fetched from the gateway,
// so it mirrors the website's profiles exactly), shows which techniques are on,
// streams the recent context_events, and lets the user switch the active
// preset. The Phase-4 plan is to swap this hand-rolled HTML for the website's
// React components (ui/components/context-profiles.tsx) bundled into the webview.

import * as vscode from 'vscode';
import { AcmClient } from './acmClient';

export class SettingsPanel {
  public static current: SettingsPanel | undefined;

  static show(client: AcmClient): void {
    if (SettingsPanel.current) {
      SettingsPanel.current.panel.reveal(vscode.ViewColumn.Active);
      void SettingsPanel.current.refresh();
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      'acmSettings',
      'ACM Context Management',
      vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    SettingsPanel.current = new SettingsPanel(panel, client);
  }

  private constructor(private panel: vscode.WebviewPanel, private client: AcmClient) {
    this.panel.onDidDispose(() => (SettingsPanel.current = undefined));
    this.panel.webview.onDidReceiveMessage(
      async (msg: { type: string; name?: string; fp?: string; conv?: string }) => {
        try {
          if (msg.type === 'setProfile' && msg.name) {
            await this.client.setProfile(msg.name);
            vscode.window.showInformationMessage(`ACM profile set to '${msg.name}'.`);
          } else if (msg.type === 'drop' && msg.fp) {
            await this.client.dropMessage(msg.fp, msg.conv ?? '');
          } else if (msg.type === 'restore' && msg.fp) {
            await this.client.restoreMessage(msg.fp, msg.conv ?? '');
          } else if (msg.type === 'setProvider' && msg.name) {
            await this.client.setDefaultProvider(msg.name);
          }
        } catch (e) {
          vscode.window.showErrorMessage(`ACM: ${(e as Error).message}`);
        }
        await this.refresh();
      },
    );
    void this.refresh();
  }

  private async refresh(): Promise<void> {
    try {
      const [profile, status, msgs, providers] = await Promise.all([
        this.client.getProfile(),
        this.client.status(),
        this.client.messages('').catch(() => ({ conversation: '', messages: [] })),
        this.client.providers().catch(() => ({ default: null, providers: {} })),
      ]);
      this.panel.webview.html = this.render(profile, status, msgs, providers);
    } catch (e) {
      this.panel.webview.html = this.renderError((e as Error).message);
    }
  }

  private render(profile: any, status: any, msgs: any, providers: any): string {
    const techniques = Object.entries(status.techniques ?? {})
      .map(([k, v]) => `<tr><td>${k}</td><td><code>${String(v)}</code></td></tr>`)
      .join('');
    const presets = (profile.presets ?? [])
      .map(
        (p: any) =>
          `<div class="preset"><button onclick="setProfile('${p.name}')">${p.name}</button>` +
          `<span>${p.summary ?? ''}</span></div>`,
      )
      .join('');
    const events = (status.last_events ?? [])
      .slice(-12)
      .reverse()
      .map((e: any) => `<li><code>${e.type}</code> ${e.freed_tokens ? `freed ~${e.freed_tokens}t` : ''}</li>`)
      .join('');
    const conv = msgs?.conversation ?? '';
    const rows = (msgs?.messages ?? [])
      .map((m: any) => {
        const btn = m.dropped
          ? `<button onclick="restore('${m.fp}')">↩ restore</button>`
          : `<button onclick="drop('${m.fp}')">🗑 remove</button>`;
        const cls = m.dropped ? ' class="dropped"' : '';
        return `<tr${cls}><td>${btn}</td><td><code>${m.role}</code></td><td>${this.esc(m.preview)}</td></tr>`;
      })
      .join('');
    const provEntries = Object.entries(providers?.providers ?? {});
    const provRows = provEntries
      .map(([slug, c]: any) => {
        const isDefault = providers.default === slug;
        const star = isDefault ? '★ ' : '';
        const btn = isDefault ? '' : `<button onclick="setProvider('${slug}')">make default</button>`;
        return `<tr><td>${star}<code>${slug}</code></td><td>${c.type}</td><td><code>${this.esc(c.api_key ?? '—')}</code></td><td>${btn}</td></tr>`;
      })
      .join('');
    return this.shell(`
      <h2>ACM Context Management</h2>
      <p class="muted">Gateway: <code>${status.upstream ?? '?'}</code> · config: <code>${profile.config_path ?? '?'}</code></p>
      <h3>Active techniques</h3>
      <table>${techniques}</table>
      <h3>Providers <span class="muted">(default: ${providers?.default ?? 'env fallback'})</span></h3>
      <table>${provRows || '<tr><td class="muted">none configured — using env upstream</td></tr>'}</table>
      <p class="muted">Add providers via the gateway API or the <code>add_provider</code> MCP tool.</p>
      <h3>Presets</h3>
      ${presets}
      <h3>Recent edits</h3>
      <ul>${events || '<li class="muted">none yet</li>'}</ul>
      <h3>Context messages <span class="muted">(${conv || 'no conversation yet'})</span></h3>
      <p class="muted">Removing a message hides it from the model on every future turn. The IDE's own transcript still shows it.</p>
      <table>${rows || '<tr><td class="muted">none seen yet — send a turn through the gateway</td></tr>'}</table>
      <input type="hidden" id="conv" value="${this.esc(conv)}">
      <p><button onclick="refresh()">Refresh</button></p>
    `);
  }

  private esc(s: string): string {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  private renderError(message: string): string {
    return this.shell(`
      <h2>ACM Context Management</h2>
      <p class="error">Can't reach the gateway.</p>
      <pre>${message}</pre>
      <p class="muted">Start it with <code>acm-gateway</code> and check <code>acm.gatewayUrl</code>.</p>
      <button onclick="refresh()">Retry</button>
    `);
  }

  private shell(body: string): string {
    return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      body { font-family: var(--vscode-font-family); padding: 12px; color: var(--vscode-foreground); }
      table { border-collapse: collapse; } td { padding: 2px 12px 2px 0; }
      .preset { margin: 4px 0; } .preset button { margin-right: 8px; }
      .muted { opacity: 0.7; } .error { color: var(--vscode-errorForeground); }
      .dropped { opacity: 0.45; text-decoration: line-through; }
      button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 4px 10px; border-radius: 4px; cursor: pointer; }
      code { color: var(--vscode-textPreformat-foreground); }
    </style></head><body>${body}
    <script>
      const vscode = acquireVsCodeApi();
      function conv() { const el = document.getElementById('conv'); return el ? el.value : ''; }
      function setProfile(name) { vscode.postMessage({ type: 'setProfile', name }); }
      function setProvider(name) { vscode.postMessage({ type: 'setProvider', name }); }
      function drop(fp) { vscode.postMessage({ type: 'drop', fp, conv: conv() }); }
      function restore(fp) { vscode.postMessage({ type: 'restore', fp, conv: conv() }); }
      function refresh() { vscode.postMessage({ type: 'refresh' }); }
    </script></body></html>`;
  }
}
