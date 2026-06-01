// Thin HTTP client for the local acm-gateway. The VSCode extension runs in
// Node (it can't import the Python engine), so everything goes over the
// gateway's control-plane endpoints (/status, /profile, /memory, /compact).

import * as http from 'http';
import { URL } from 'url';

export interface AcmStatus {
  ok: boolean;
  upstream: string;
  config_path: string;
  tool_surface: string;
  techniques: Record<string, unknown>;
  last_events: Array<Record<string, unknown>>;
}

export interface AcmProfile {
  active: Record<string, unknown>;
  config_path: string;
  presets: Array<{ name: string; summary: string | null; body: Record<string, unknown> }>;
}

export class AcmClient {
  constructor(private baseUrl: string) {}

  private request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const url = new URL(path, this.baseUrl);
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const opts: http.RequestOptions = {
      method,
      hostname: url.hostname,
      port: url.port,
      path: url.pathname + url.search,
      headers: {
        'Content-Type': 'application/json',
        ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
      },
    };
    return new Promise<T>((resolve, reject) => {
      const req = http.request(opts, (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c as Buffer));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if ((res.statusCode ?? 500) >= 400) {
            reject(new Error(`acm-gateway ${res.statusCode}: ${text}`));
            return;
          }
          try {
            resolve(text ? (JSON.parse(text) as T) : ({} as T));
          } catch (e) {
            reject(e as Error);
          }
        });
      });
      req.on('error', reject);
      if (payload) {
        req.write(payload);
      }
      req.end();
    });
  }

  status(): Promise<AcmStatus> {
    return this.request<AcmStatus>('GET', '/status');
  }

  getProfile(): Promise<AcmProfile> {
    return this.request<AcmProfile>('GET', '/profile');
  }

  setProfile(name: string): Promise<{ ok: boolean }> {
    return this.request('POST', '/profile', { name });
  }

  remember(text: string, scope = 'user'): Promise<{ ok: boolean; count: number }> {
    return this.request('POST', '/memory/remember', { text, scope });
  }

  recall(query = '', scope = 'user', limit = 10): Promise<{ items: string[] }> {
    const q = `?query=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}&limit=${limit}`;
    return this.request('GET', '/memory/recall' + q);
  }

  compact(text: string): Promise<{ summary: string }> {
    return this.request('POST', '/compact', { text });
  }

  // ── manual message removal (drop-list) ──────────────────────────────
  conversations(): Promise<{ conversations: Array<{ key: string; count: number; dropped: number; ts: number }> }> {
    return this.request('GET', '/conversations');
  }

  messages(conv = ''): Promise<{ conversation: string; messages: AcmMessageRow[] }> {
    return this.request('GET', '/messages?conv=' + encodeURIComponent(conv));
  }

  dropMessage(fp: string, conv = ''): Promise<{ ok: boolean }> {
    return this.request('POST', '/messages/drop', { fp, conv: conv || null });
  }

  restoreMessage(fp: string, conv = ''): Promise<{ ok: boolean }> {
    return this.request('POST', '/messages/restore', { fp, conv: conv || null });
  }

  // ── multi-provider ──────────────────────────────────────────────────
  providers(): Promise<{ default: string | null; providers: Record<string, any> }> {
    return this.request('GET', '/providers');
  }

  setDefaultProvider(slug: string): Promise<{ ok: boolean }> {
    return this.request('POST', `/providers/${encodeURIComponent(slug)}/default`);
  }
}

export interface AcmMessageRow {
  fp: string;
  role: string;
  preview: string;
  tool_call_id: string;
  dropped: boolean;
}
