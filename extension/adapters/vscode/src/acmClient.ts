// Thin HTTP client for the local acm-gateway. The VSCode extension runs in
// Node (it can't import the Python engine), so everything goes over the
// gateway's control-plane endpoints (/status, /profile, /memory, /compact).

import * as http from 'http';
import { URL } from 'url';

export interface AcmAuth {
  /** The configured ACM_ANTHROPIC_AUTH_MODE (auto | passthrough | api_key). */
  configured_mode: string;
  /** What the last Anthropic turn actually used (passthrough | api_key). */
  mode: string;
  /** True when the last turn forwarded the user's own subscription bearer. */
  subscription: boolean;
  token_tail: string | null;
}

export interface AcmContext {
  conversation: string;
  tokens: number;
  saved_tokens: number;
  messages: number;
  dropped: number;
}

export interface AcmStatus {
  ok: boolean;
  upstream: string;
  config_path: string;
  tool_surface: string;
  techniques: Record<string, unknown>;
  last_events: Array<Record<string, unknown>>;
  context?: AcmContext;
  auth?: AcmAuth;
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

  setProfileBody(body: Record<string, unknown>, visual_method?: unknown): Promise<{ ok: boolean }> {
    return this.request('POST', '/profile', { body, visual_method });
  }

  remember(text: string, scope = 'user'): Promise<{ ok: boolean; count: number }> {
    return this.request('POST', '/memory/remember', { text, scope });
  }

  recall(query = '', scope = 'user', limit = 10): Promise<{ items: string[] }> {
    const q = `?query=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}&limit=${limit}`;
    return this.request('GET', '/memory/recall' + q);
  }

  memoryClear(scope = 'user'): Promise<{ ok: boolean }> {
    return this.request('POST', '/memory/clear', { scope });
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

  // The exact payload last forwarded upstream (post-pipeline) for a conversation.
  contextWindow(conv = ''): Promise<AcmContextWindow> {
    return this.request<AcmContextWindow>(
      'GET',
      '/context_window?conv=' + encodeURIComponent(conv),
    );
  }

  dropMessage(fp: string, conv = ''): Promise<{ ok: boolean }> {
    return this.request('POST', '/messages/drop', { fp, conv: conv || null });
  }

  restoreMessage(fp: string, conv = ''): Promise<{ ok: boolean }> {
    return this.request('POST', '/messages/restore', { fp, conv: conv || null });
  }

  dropMany(fps: string[], conv = ''): Promise<{ ok: boolean; dropped: string[] }> {
    return this.request('POST', '/messages/drop_many', { fps, conv: conv || null });
  }

  // ── relevance pruning (task-aware suggestions) ──────────────────────
  relevanceSuggest(conv = ''): Promise<RelevanceResult> {
    return this.request<RelevanceResult>('GET', '/relevance/suggest?conv=' + encodeURIComponent(conv));
  }

  relevanceFeedback(payload: Record<string, unknown>): Promise<{ ok: boolean }> {
    return this.request('POST', '/relevance/feedback', payload);
  }

  relevanceSummarize(payload: {
    member_fps: string[];
    conv?: string;
    title?: string;
    model?: string;
  }): Promise<{ ok: boolean; summary?: string; error?: string }> {
    return this.request('POST', '/relevance/summarize', payload);
  }

  messageImages(
    fp: string,
    conv = '',
  ): Promise<{ images: string[]; count: number; error?: string }> {
    return this.request(
      'GET',
      '/messages/images?conv=' +
        encodeURIComponent(conv) +
        '&fp=' +
        encodeURIComponent(fp),
    );
  }

  messageText(fp: string, conv = ''): Promise<{ text: string; error?: string }> {
    return this.request(
      'GET',
      '/messages/text?conv=' +
        encodeURIComponent(conv) +
        '&fp=' +
        encodeURIComponent(fp),
    );
  }

  // ── context windows (one per chat: per-chat profile + lifecycle) ─────
  contextWindows(project = ''): Promise<{ windows: AcmContextWindowRow[] }> {
    const q = project ? '?project=' + encodeURIComponent(project) : '';
    return this.request('GET', '/context_windows' + q);
  }

  getContextWindow(conv: string): Promise<AcmContextWindowRow & { profile?: Record<string, unknown> }> {
    return this.request('GET', `/context_windows/${encodeURIComponent(conv)}`);
  }

  setWindowProfile(
    conv: string,
    sel: { name?: string; body?: Record<string, unknown>; clear?: boolean },
  ): Promise<AcmContextWindowRow> {
    return this.request('POST', `/context_windows/${encodeURIComponent(conv)}/profile`, sel);
  }

  deleteWindow(conv: string): Promise<{ ok: boolean; deleted: boolean; conversation: string }> {
    return this.request('DELETE', `/context_windows/${encodeURIComponent(conv)}`);
  }

  // ── multi-provider ──────────────────────────────────────────────────
  providers(): Promise<{ default: string | null; providers: Record<string, any> }> {
    return this.request('GET', '/providers');
  }

  setDefaultProvider(slug: string): Promise<{ ok: boolean }> {
    return this.request('POST', `/providers/${encodeURIComponent(slug)}/default`);
  }

  upsertProvider(cfg: Record<string, unknown>): Promise<{ ok: boolean }> {
    return this.request('POST', '/providers', cfg);
  }

  deleteProvider(slug: string): Promise<{ ok: boolean }> {
    return this.request('DELETE', `/providers/${encodeURIComponent(slug)}`);
  }
}

export interface AcmMessageRow {
  fp: string;
  role: string;
  preview: string;
  tool_call_id: string;
  dropped: boolean;
}

// One chat's context window: its effective profile + live stats.
export interface AcmContextWindowRow {
  id: string;
  title: string;
  project: string;
  profile_name: string | null;
  profile_source: 'global' | 'preset' | 'body';
  tokens: number;
  messages: number;
  dropped: number;
  pinned: boolean;
  last_seen: number;
  techniques: Record<string, unknown>;
}

// The exact wire body the gateway last forwarded upstream for a conversation.
// `messages` / `system` / `tools` are raw provider-shaped JSON (OpenAI or
// Anthropic, per `surface`); the UI normalises them for display.
export interface AcmContextWindow {
  conversation: string;
  ts: number;
  surface: '' | 'openai' | 'anthropic';
  model: string;
  system: unknown;
  messages: any[];
  tools: any[];
}

export interface RelevanceSuggestion {
  episode_id: string;
  episode_index: number;
  label: 'KEEP' | 'SUMMARIZE' | 'DROP';
  score: number;
  reason: string;
  source: 'encoder' | 'judge' | 'ensemble' | 'rule';
  freed_tokens: number;
  member_indices: number[];
  member_fps: string[];
  title: string;
  dropped: boolean;
}

export interface RelevanceResult {
  conversation: string;
  suggestions: RelevanceSuggestion[];
  info: Record<string, number>;
  error?: string;
}
