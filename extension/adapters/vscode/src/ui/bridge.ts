// Webview-side RPC bridge: rpc('method', params) posts a message to the
// extension host and resolves when the matching reply arrives. The host runs the
// actual HTTP call to the gateway (see webview.ts).

const vscode = acquireVsCodeApi();

let seq = 0;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();

window.addEventListener('message', (e: MessageEvent) => {
  const m = e.data;
  if (m && m.type === 'rpc-result') {
    const p = pending.get(m.id);
    if (p) {
      pending.delete(m.id);
      m.ok ? p.resolve(m.data) : p.reject(new Error(m.error || 'rpc error'));
    }
  }
});

export function rpc<T = any>(method: string, params?: any): Promise<T> {
  const id = ++seq;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve, reject });
    vscode.postMessage({ type: 'rpc', id, method, params: params || {} });
  });
}

export const mount: string = (window as Window).acmMount || 'panel';

// Persisted webview state (theme choice, last tab, …). acquireVsCodeApi can only
// be called once, so these reuse the single instance above.
export function getState(): any {
  return (vscode.getState() as any) || {};
}
export function setState(patch: Record<string, unknown>): void {
  vscode.setState({ ...getState(), ...patch });
}
