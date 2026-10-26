// Webview-side RPC bridge: rpc('method', params) posts a message to the
// extension host and resolves when the matching reply arrives. The host runs the
// actual HTTP call to the gateway (see webview.ts).

import { useEffect } from 'react';

const vscode = acquireVsCodeApi();

let seq = 0;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();

// Realtime events relayed from the gateway by the host (see startEventRelay in
// webview.ts). Views subscribe via useAcmEvents to refresh the instant a turn
// flows through, instead of polling.
export type AcmEvent = { type: string; conv?: string; project?: string; action?: string; running?: boolean };
const eventListeners = new Set<(event: AcmEvent) => void>();

window.addEventListener('message', (e: MessageEvent) => {
  const m = e.data;
  if (m && m.type === 'rpc-result') {
    const p = pending.get(m.id);
    if (p) {
      pending.delete(m.id);
      m.ok ? p.resolve(m.data) : p.reject(new Error(m.error || 'rpc error'));
    }
  } else if (m && m.type === 'acm-event' && m.event) {
    for (const fn of eventListeners) {
      fn(m.event as AcmEvent);
    }
  }
});

/**
 * Subscribe to realtime gateway events. `handler` should be stable or wrapped
 * in useCallback; it fires for every event, so callers filter by `conv`/`type`.
 */
export function useAcmEvents(handler: (event: AcmEvent) => void): void {
  useEffect(() => {
    eventListeners.add(handler);
    return () => {
      eventListeners.delete(handler);
    };
  }, [handler]);
}

export function rpc<T = any>(method: string, params?: any): Promise<T> {
  const id = ++seq;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve, reject });
    vscode.postMessage({ type: 'rpc', id, method, params: params || {} });
  });
}

// Ask the host to open a chat's two-column detail in an editor tab.
export function openChat(conv: string): void {
  vscode.postMessage({ type: 'open-chat', conv });
}

export const mount: string = (window as Window).acmMount || 'panel';
// The current project root + (for the chat tab) which chat to show — injected by
// the host into the webview HTML.
export const projectRoot: string = (window as any).acmProject || '';

// The gateway base URL this IDE points at — shown in the onboarding flow.
export const gatewayUrl: string = (window as any).acmGateway || 'http://127.0.0.1:8807';
export const chatConv: string = (window as any).acmChat || '';

// Persisted webview state (theme choice, last tab, …). acquireVsCodeApi can only
// be called once, so these reuse the single instance above.
export function getState(): any {
  return (vscode.getState() as any) || {};
}
export function setState(patch: Record<string, unknown>): void {
  vscode.setState({ ...getState(), ...patch });
}
