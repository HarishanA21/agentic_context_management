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
  budget?: number;
  budget_pct?: number;
  over_warn?: boolean;
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

  /**
   * Subscribe to the gateway's realtime event stream (Server-Sent Events).
   * Calls `onEvent` for each parsed event. Returns a disposer that closes the
   * connection. The webview can't open a socket itself (its CSP forbids it), so
   * the host owns this one connection and relays events to its webviews.
   */
  events(onEvent: (event: Record<string, unknown>) => void): () => void {
    const url = new URL('/events', this.baseUrl);
    let req: http.ClientRequest | undefined;
    let res: http.IncomingMessage | undefined;
    let closed = false;
    let retry: NodeJS.Timeout | undefined;

    const connect = () => {
      if (closed) {
        return;
      }
      req = http.request(
        {
          method: 'GET',
          hostname: url.hostname,
          port: url.port,
          path: url.pathname + url.search,
          headers: { Accept: 'text/event-stream' },
        },
        (r) => {
          res = r;
          if ((r.statusCode ?? 500) >= 400) {
            r.resume();
            scheduleReconnect();
            return;
          }
          r.setEncoding('utf8');
          let buf = '';
          r.on('data', (chunk: string) => {
            buf += chunk;
            // SSE frames are separated by a blank line.
            let sep: number;
            while ((sep = buf.indexOf('\n\n')) !== -1) {
              const frame = buf.slice(0, sep);
              buf = buf.slice(sep + 2);
              for (const line of frame.split('\n')) {
                if (!line.startsWith('data:')) {
                  continue; // skip comments (heartbeats) and other fields
                }
                const data = line.slice(5).trim();
                if (!data) {
                  continue;
                }
                try {
                  onEvent(JSON.parse(data) as Record<string, unknown>);
                } catch {
                  /* ignore malformed frame */
                }
              }
            }
          });
          r.on('end', scheduleReconnect);
          r.on('error', scheduleReconnect);
        },
      );
      req.on('error', scheduleReconnect);
      req.end();
    };

    const scheduleReconnect = () => {
      if (closed || retry) {
        return;
      }
      retry = setTimeout(() => {
        retry = undefined;
        connect();
      }, 2000);
    };

    connect();

    return () => {
      closed = true;
      if (retry) {
        clearTimeout(retry);
      }
      res?.destroy();
      req?.destroy();
    };
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

  // ── savings dashboard (aggregated freed tokens) ─────────────────────
  savings(): Promise<AcmSavings> {
    return this.request<AcmSavings>('GET', '/savings');
  }

  // ── preview (dry-run the pipeline on the next request) ──────────────
  preview(conv = ''): Promise<AcmPreview> {
    const q = conv ? `?conv=${encodeURIComponent(conv)}` : '';
    return this.request<AcmPreview>('GET', `/preview${q}`);
  }

  savingsReset(conv = ''): Promise<{ ok: boolean }> {
    return this.request('POST', '/savings/reset', conv ? { conversation: conv } : {});
  }

  // ── training-data export (relevance feedback → trainer files) ───────
  trainingSummary(includeModelLabels = false): Promise<AcmTrainingSummary> {
    const q = includeModelLabels ? '?include_model_labels=1' : '';
    return this.request<AcmTrainingSummary>('GET', `/training/summary${q}`);
  }

  trainingExport(includeModelLabels = false, dir = ''): Promise<AcmTrainingManifest> {
    const body: Record<string, unknown> = {};
    if (includeModelLabels) body.include_model_labels = true;
    if (dir) body.dir = dir;
    return this.request<AcmTrainingManifest>('POST', '/training/export', body);
  }

  // ── undo (reverse the last manual edit) ─────────────────────────────
  undoStatus(conv = ''): Promise<AcmUndoStatus> {
    const q = conv ? `?conv=${encodeURIComponent(conv)}` : '';
    return this.request<AcmUndoStatus>('GET', `/undo${q}`);
  }

  undo(conv = ''): Promise<AcmUndoResult> {
    return this.request<AcmUndoResult>('POST', '/undo', conv ? { conv } : {});
  }

  // ── manual message removal (drop-list) ──────────────────────────────
  conversations(): Promise<{ conversations: Array<{ key: string; count: number; dropped: number; ts: number }> }> {
    return this.request('GET', '/conversations');
  }

  messages(
    conv = '',
    full = false,
  ): Promise<{ conversation: string; messages: AcmMessageRow[] }> {
    return this.request(
      'GET',
      '/messages?conv=' + encodeURIComponent(conv) + (full ? '&full=1' : ''),
    );
  }

  // The exact payload last forwarded upstream (post-pipeline) for a conversation.
  contextWindow(conv = ''): Promise<AcmContextWindow> {
    return this.request<AcmContextWindow>(
      'GET',
      '/context_window?conv=' + encodeURIComponent(conv),
    );
  }

  // Per-request composition history (Graph view): one entry per proxied turn.
  contextTimeline(conv = '', limit = 50): Promise<AcmContextTimeline> {
    return this.request<AcmContextTimeline>(
      'GET',
      '/context_timeline?conv=' + encodeURIComponent(conv) + '&limit=' + limit,
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

  resetWindows(): Promise<{ ok: boolean; cleared: number }> {
    return this.request('POST', '/context_windows/reset');
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
  tokens?: number;
  // True when the message carries image block(s) — a tool screenshot or a
  // visual-method rasterised page. The conversation view fetches and renders
  // these inline via messageImages(fp).
  has_image?: boolean;
  // Present only when the row was fetched with full=1 — the complete message
  // text, so the conversation view can render in order without a per-row fetch.
  text?: string;
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

// One message block in a timeline turn (Graph view). `tokens` is the block's
// size before the pipeline ran; `after_tokens` is set when status is 'changed'.
export interface AcmTimelineBlock {
  id: string | null;
  fp: string;
  role: string; // system | human | ai | tool
  tokens: number;
  after_tokens?: number;
  preview: string;
  status: 'kept' | 'changed' | 'removed' | 'added';
  technique: string; // '' when kept
}

export interface AcmTimelineTurn {
  index: number;
  ts: number;
  surface: string;
  model: string;
  before_tokens: number;
  after_tokens: number;
  new_fps: string[]; // fps new since the previous turn (the growth blocks)
  events: Array<Record<string, unknown>>;
  blocks: AcmTimelineBlock[];
}

export interface AcmContextTimeline {
  conversation: string;
  limit: number;
  turns: AcmTimelineTurn[];
}

export interface AcmSavingsRow {
  conversation: string;
  title: string;
  freed_tokens: number;
  turns: number;
  by_technique: Record<string, number>;
  last_ts: number | null;
  cost_saved: number;
}

export interface AcmSavings {
  total_freed_tokens: number;
  total_turns: number;
  total_cost_saved: number;
  cost_per_mtok: number;
  by_technique: Record<string, number>;
  conversations: AcmSavingsRow[];
}

export interface AcmTrainingSummary {
  encoder_examples: number;
  gold_examples: number;
  silver_examples: number;
  judge_pairs: number;
  label_counts: Record<string, number>;
  override_rate: number;
  error?: string;
}

export interface AcmTrainingManifest extends AcmTrainingSummary {
  ok: boolean;
  dir: string;
  encoder_path: string;
  judge_path: string;
}

export interface AcmUndoTop {
  kind: 'drop' | 'drop_many' | 'restore' | 'summarize';
  label: string;
  depth: number;
}

export interface AcmUndoStatus {
  conversation: string;
  top: AcmUndoTop | null;
}

export interface AcmUndoResult {
  ok: boolean;
  conversation: string;
  reason?: string;
  undone?: { kind: string; label: string };
  dropped?: string[];
  top?: AcmUndoTop | null;
}

export interface AcmPreviewRow {
  fp: string;
  role: string;
  preview: string;
  tokens: number;
  status: 'kept' | 'changed' | 'removed' | 'added';
  after_preview?: string;
  after_tokens?: number;
}

export interface AcmPreview {
  conversation: string;
  available: boolean;
  reason?: string;
  before_tokens?: number;
  after_tokens?: number;
  freed_tokens?: number;
  before_messages?: number;
  after_messages?: number;
  rows?: AcmPreviewRow[];
  events?: any[];
  summarization_pending?: boolean;
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
