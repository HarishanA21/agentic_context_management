import { useEffect, useState, useCallback, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import { rpc, getState, setState, openChat, projectRoot, chatConv, useAcmEvents } from './bridge';
import type { AcmEvent } from './bridge';
import { Onboarding } from './Onboarding';

type Theme = 'auto' | 'light' | 'dark';
const THEME_ICON: Record<Theme, string> = { auto: '◐', light: '☀', dark: '☾' };
const NEXT_THEME: Record<Theme, Theme> = { auto: 'light', light: 'dark', dark: 'auto' };

const clone = <T,>(o: T): T => JSON.parse(JSON.stringify(o));
const useReload = (): [number, () => void] => {
  const [n, setN] = useState(0);
  return [n, useCallback(() => setN((x) => x + 1), [])];
};
// A chat's conversation key is a long hash (e.g. "s7f3a..._c91b2..."). The full
// string is noise in the UI; this distils it to a short, stable handle like
// "#c91b2" so each chat has a readable identifier next to its title without two
// chats ever colliding visually.
function shortId(conv: string): string {
  if (!conv) return '#????';
  const tail = conv.includes('_') ? conv.slice(conv.lastIndexOf('_') + 1) : conv;
  return '#' + tail.replace(/^c/, '').slice(0, 5);
}

function rel(ts: number): string {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 45) return 'just now';
  if (s < 90) return 'a minute ago';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 5400) return 'an hour ago';
  if (s < 86400) return Math.floor(s / 3600) + ' hours ago';
  if (s < 172800) return 'yesterday';
  return Math.floor(s / 86400) + ' days ago';
}

// A chat is "active" if it saw traffic recently — the same idea as Claude Code
// dimming an idle session. Under this window it's live; past it, dormant.
const ACTIVE_WINDOW_S = 15 * 60;

// Three-state health for a chat's status dot:
//   'running'  — a turn is generating upstream right now  → green, blinking
//   'active'   — used within ACTIVE_WINDOW_S, but idle now → yellow
//   'idle'     — dormant                                   → red, steady
function chatState(c: any, running: boolean): 'running' | 'active' | 'idle' {
  if (running) return 'running';
  const last = Number(c.last_seen || 0);
  if (last && Date.now() / 1000 - last < ACTIVE_WINDOW_S) return 'active';
  return 'idle';
}

// A stable, distinct accent per session so different chats read apart at a
// glance. We hash the conversation id to a hue on the colour wheel — same id →
// same colour every render, no palette to run out of.
function sessionHue(conv: string): number {
  let h = 0;
  for (let i = 0; i < conv.length; i++) h = (h * 31 + conv.charCodeAt(i)) >>> 0;
  return h % 360;
}
function sessionColor(conv: string): string {
  return `hsl(${sessionHue(conv)} 65% 55%)`;
}
// Claude Code injects its system prompt, <system-reminder> blocks, agent-type
// lists and claudeMd as *user*-role messages. Tag those as "Context" so they're
// visually separated from your real prompts and don't each become their own turn.
const CONTEXT_RE =
  /^\s*(<system-reminder>|Available agent types|#\s*claudeMd|x-anthropic-billing-header|You are Claude Code|<command-|<local-command)/i;
function classify(m: any): { cls: string; label: string; context: boolean } {
  const r = String(m.role || '').toLowerCase();
  if (r === 'ai' || r === 'assistant') return { cls: 'assistant', label: 'Assistant', context: false };
  if (r === 'tool') return { cls: 'tool', label: 'Tool', context: false };
  if (r === 'human' || r === 'user') {
    return CONTEXT_RE.test(String(m.preview || ''))
      ? { cls: 'context', label: 'Context', context: true }
      : { cls: 'user', label: 'User', context: false };
  }
  return { cls: 'system', label: 'System', context: true };
}

// Claude Code packs a lot into a single user-role message: your actual prompt
// plus injected context (system reminders, slash-command echoes, the agent-type
// and skills listings, claudeMd, hook notes). On the Anthropic wire there's no
// "context" role, so it all arrives as one HumanMessage. We can't split it on
// the wire (removal is per whole message), but we CAN split it for display so
// your real prompt stands apart from the noise.
type CtxPart = { kind: 'prompt' | 'context'; label: string; text: string };

// Well-delimited XML-ish blocks Claude Code injects. One regex, backreference
// closes the matching tag, so each block is captured individually and in order.
const CTX_TAG_RE =
  /<(system-reminder|command-name|command-message|command-args|command-contents|local-command-stdout|local-command-stderr|local-command-caveat)>[\s\S]*?<\/\1>/g;
function tagLabel(tag: string): string {
  if (tag === 'system-reminder') return 'System reminder';
  if (tag.startsWith('command')) return 'Slash command';
  if (tag === 'local-command-stdout' || tag === 'local-command-stderr') return 'Command output';
  if (tag === 'local-command-caveat') return 'Caveat';
  return 'Context';
}
// Plain-text (untagged) blocks are recognised by their leading header line.
const PROSE_CTX: { re: RegExp; label: string }[] = [
  { re: /^available agent types/i, label: 'Agent types' },
  { re: /^when (?:using|you launch) the agent tool/i, label: 'Agent types' },
  { re: /^the following skills are available/i, label: 'Skills' },
  { re: /^#\s*claudemd/i, label: 'Project instructions' },
  { re: /^codebase and user instructions/i, label: 'Project instructions' },
  { re: /^contents of .+memory/i, label: 'Memory notes' },
  { re: /^userpromptsubmit hook/i, label: 'Hook context' },
  { re: /^you are claude code/i, label: 'System context' },
  { re: /^caveat:/i, label: 'Caveat' },
];

// Break one user-role message's text into ordered, labeled parts. Whatever
// isn't a recognised context block is treated as your actual prompt.
function splitUserParts(text: string): CtxPart[] {
  const parts: CtxPart[] = [];
  const push = (kind: CtxPart['kind'], label: string, t: string) => {
    const s = t.trim();
    if (s) parts.push({ kind, label, text: s });
  };
  const emitGap = (gap: string) => {
    for (const chunk of gap.split(/\n{2,}/)) {
      const s = chunk.trim();
      if (!s) continue;
      const hit = PROSE_CTX.find((p) => p.re.test(s));
      if (hit) push('context', hit.label, s);
      else push('prompt', 'Your message', s);
    }
  };
  let last = 0;
  let m: RegExpExecArray | null;
  CTX_TAG_RE.lastIndex = 0;
  while ((m = CTX_TAG_RE.exec(text))) {
    emitGap(text.slice(last, m.index));
    push('context', tagLabel(m[1]), m[0]);
    last = m.index + m[0].length;
  }
  emitGap(text.slice(last));
  // Collapse runs of the same label into one part so the view isn't fragmented.
  const merged: CtxPart[] = [];
  for (const p of parts) {
    const prev = merged[merged.length - 1];
    if (prev && prev.kind === p.kind && prev.label === p.label) prev.text += '\n\n' + p.text;
    else merged.push({ ...p });
  }
  return merged;
}

const TABS = ['Overview', 'Savings', 'Chats', 'Techniques', 'Providers', 'Memory', 'Training'];

// Per-label colour for relevance suggestion cards (theme-agnostic alpha fills).
const LABEL_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  DROP: { bg: 'rgba(220,80,80,0.16)', fg: '#e06c6c', label: 'Drop' },
  SUMMARIZE: { bg: 'rgba(210,160,60,0.16)', fg: '#d2a03c', label: 'Summarize' },
  KEEP: { bg: 'rgba(90,180,120,0.16)', fg: '#5ab478', label: 'Keep' },
};

// One-word status derived from the judge's reason, so the list reads at a
// glance (done / error / duplicate / empty / active) instead of a sentence.
function statusWord(s: any): { w: string; bg: string; fg: string } {
  const r = String(s.reason || '').toLowerCase();
  if (/(fail|error|crash|exception|denied)/.test(r)) return { w: 'error', bg: 'rgba(220,80,80,0.16)', fg: '#e06c6c' };
  if (/(duplicate|redundant|repeat)/.test(r)) return { w: 'duplicate', bg: 'rgba(210,160,60,0.16)', fg: '#d2a03c' };
  if (/(empty|no-op|nothing|blank)/.test(r)) return { w: 'empty', bg: 'rgba(150,150,150,0.16)', fg: '#999' };
  if (/(in progress|pending|ongoing|working)/.test(r)) return { w: 'active', bg: 'rgba(90,150,220,0.16)', fg: '#5a96dc' };
  if (/(success|added|complete|done|implement|created|updated|wrote|finish)/.test(r)) return { w: 'done', bg: 'rgba(90,180,120,0.16)', fg: '#5ab478' };
  return { w: String(s.label || '').toLowerCase(), bg: 'rgba(150,150,150,0.12)', fg: '#999' };
}

const TECHS = [
  { key: 'tool_result_trimming', name: 'Tool-result trimming', desc: 'Replace old, large tool outputs with a short placeholder once the chat passes a token threshold.', params: [['trigger_tokens', 'Trigger (tokens)'], ['keep_recent', 'Keep recent']] },
  { key: 'summarization', name: 'Summarisation', desc: 'Compress older turns into a short summary when the conversation grows long.', params: [['trigger_tokens', 'Trigger (tokens)'], ['keep_recent', 'Keep recent']] },
  { key: 'sliding_window', name: 'Sliding window', desc: 'Drop the middle of very long chats, keeping the system prompt and the most recent turns.', params: [['keep_recent', 'Keep recent']] },
  { key: 'memory', name: 'Memory', desc: 'Let the agent store and recall notes across turns and sessions.', params: [] as string[][] },
  { key: 'subagent', name: 'Sub-agents', desc: 'Delegate heavy sub-tasks to an isolated agent; only its summary returns to the main chat.', params: [] as string[][] },
  { key: 'jit_tools', name: 'JIT tools', desc: 'Load files on demand (find, grep, read-slice) instead of dumping everything up front.', params: [] as string[][] },
];

export function App() {
  const [tab, setTab] = useState('Overview');
  const [status, setStatus] = useState<any>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [theme, setTheme] = useState<Theme>(() => (getState().theme as Theme) || 'auto');
  // First-run onboarding: show until the user gets started, then never again
  // (persisted in webview state). They can reopen it from the header.
  const [onboarded, setOnboarded] = useState<boolean>(() => Boolean(getState().onboarded));

  const finishOnboarding = () => {
    setOnboarded(true);
    setState({ onboarded: true });
  };

  const cycleTheme = () => {
    const next = NEXT_THEME[theme];
    setTheme(next);
    setState({ theme: next });
  };

  const poll = useCallback(() => {
    rpc('status').then((s) => { setStatus(s); setReachable(true); }).catch(() => setReachable(false));
  }, []);
  useEffect(() => {
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, [poll]);

  if (!onboarded) {
    return (
      <div className="acm" data-theme={theme}>
        <Onboarding onDone={finishOnboarding} />
      </div>
    );
  }

  return (
    <div className="acm" data-theme={theme}>
      <header className="hd">
        <svg className="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          <path d="m3 12 9 4.5L21 12M3 16.5l9 4.5 9-4.5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
        <div style={{ minWidth: 0 }}>
          <div className="title">ACM Context Management</div>
          <div className="sub">{status?.upstream || 'local gateway'}</div>
        </div>
        <div className="right">
          <button className="theme-btn" title={`Theme: ${theme} (click to change)`} onClick={cycleTheme}>
            <span aria-hidden>{THEME_ICON[theme]}</span>
            <span style={{ textTransform: 'capitalize' }}>{theme}</span>
          </button>
          <span className="pill">
            <span className={'dot ' + (reachable === null ? '' : reachable ? 'ok' : 'bad')} />
            {reachable === null ? 'Connecting…' : reachable ? 'Connected' : 'Offline'}
          </span>
        </div>
      </header>

      {reachable === false && (
        <div className="banner">
          Can't reach the gateway. Run <code>acm-gateway</code> in a terminal and check
          <code> acm.gatewayUrl</code> in Settings.
        </div>
      )}

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={t === tab ? 'active' : ''} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>

      <main className="body">
        {tab === 'Overview' && <Overview status={status} reachable={reachable} onRefresh={poll} />}
        {tab === 'Savings' && <Savings />}
        {tab === 'Chats' && <Chats />}
        {tab === 'Techniques' && <Techniques />}
        {tab === 'Providers' && <Providers />}
        {tab === 'Memory' && <Memory />}
        {tab === 'Training' && <Training />}
      </main>
    </div>
  );
}

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} />
      <span className="slider" />
    </label>
  );
}

function Loading() {
  return <p className="muted"><span className="spin" /> loading…</p>;
}

// ── Overview ───────────────────────────────────────────────────────────
function prettySurface(s: string): string {
  if (s === 'ts_code_mode') return 'TS Code Mode';
  if (s === 'tool_calling') return 'Tool calling';
  return s || '—';
}
function hostOf(url: string): string {
  try { return new URL(url).host; } catch { return url; }
}

function Overview({ status, reachable, onRefresh }: any) {
  if (reachable === false) return <p className="muted">Gateway offline — start it to see status.</p>;
  if (!status) return <Loading />;
  const tech = status.techniques || {};
  const on = (v: any) => v && v !== 'off';
  const activeCount = Object.values(tech).filter(on).length;
  const events = (status.last_events || []).slice(-12).reverse();

  const ctx = status.context || {};
  const live = Number(ctx.tokens || 0);
  const saved = Number(ctx.saved_tokens || 0);
  const notices: any[] = status.notices || [];
  const orig = live + saved;
  const livePct = orig > 0 ? Math.max(2, Math.round((live / orig) * 100)) : 100;
  const savedPct = orig > 0 ? Math.round((saved / orig) * 100) : 0;

  return (
    <div>
      {/* Degraded-mode notices — config gaps that silently weaken ACM */}
      {notices.length > 0 && (
        <div className="notices">
          {notices.map((n: any, i: number) => (
            <div key={i} className={'notice ' + (n.level === 'error' ? 'error' : 'warn')}>
              <span className="notice-dot" />
              <span>{n.message}</span>
            </div>
          ))}
        </div>
      )}

      {/* Context gauge — the headline number for a context-management tool */}
      <div className="card" style={{ padding: 14 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 26, fontWeight: 700, lineHeight: 1.1 }}>
              {fmtTok(live)}
              <span className="muted" style={{ fontSize: 13, fontWeight: 400 }}> tokens in context</span>
            </div>
            <div className="muted tiny" style={{ marginTop: 3 }}>
              {(ctx.messages || 0)} message{(ctx.messages || 0) === 1 ? '' : 's'}
              {ctx.dropped ? ` · ${ctx.dropped} removed` : ''}
              {ctx.conversation ? ` · ${ctx.conversation}` : ' · no conversation yet'}
            </div>
          </div>
          {saved > 0 && (
            <span className="badge context" title="Tokens ACM has trimmed from this conversation">
              −{fmtTok(saved)} saved
            </span>
          )}
        </div>

        {/* live vs saved bar (only meaningful once ACM has trimmed something) */}
        {saved > 0 && (
          <>
            <div style={{ display: 'flex', height: 8, borderRadius: 5, overflow: 'hidden', marginTop: 12, background: 'rgba(127,127,127,0.18)' }}>
              <div style={{ width: livePct + '%', background: 'var(--accent)' }} title={`${fmtTok(live)} live`} />
              <div style={{ width: savedPct + '%', background: 'rgba(138,109,59,0.8)' }} title={`${fmtTok(saved)} saved`} />
            </div>
            <div className="muted tiny" style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5 }}>
              <span>● live {fmtTok(live)} ({livePct}%)</span>
              <span>● saved {fmtTok(saved)} ({savedPct}%)</span>
            </div>
          </>
        )}
      </div>

      {/* Context budget meter — how close the live chat is to its ceiling.
          Hidden when the budget is disabled (ACM_CONTEXT_BUDGET=0). */}
      {Number(ctx.budget || 0) > 0 && (
        <div className="card" style={{ padding: 12 }}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span className="muted tiny">Context budget</span>
            <span className="tiny" style={{ color: ctx.over_warn ? 'var(--warn)' : undefined }}>
              {fmtTok(live)} / {fmtTok(Number(ctx.budget))} ({Number(ctx.budget_pct || 0)}%)
            </span>
          </div>
          <div style={{ height: 8, borderRadius: 5, overflow: 'hidden', marginTop: 8, background: 'rgba(127,127,127,0.18)' }}>
            <div
              style={{
                width: Math.min(100, Number(ctx.budget_pct || 0)) + '%',
                height: '100%',
                background: ctx.over_warn ? 'var(--warn)' : 'var(--accent)',
              }}
            />
          </div>
          {ctx.over_warn && (
            <div className="muted tiny" style={{ marginTop: 6 }}>
              Nearing the window limit — prune or summarize this chat to avoid an overflow.
            </div>
          )}
        </div>
      )}

      {/* Active techniques — only what's on; the full list lives in the Techniques tab */}
      <h3 className="sec">Active techniques <span className="muted tiny">({activeCount} on)</span></h3>
      {activeCount === 0 ? (
        <p className="muted tiny">Nothing enabled — turn techniques on in the Techniques tab.</p>
      ) : (
        <div className="chips">
          {Object.entries(tech).filter(([, v]) => on(v)).map(([k, v]) => (
            <span key={k} className="chip on">
              <span className="dot ok" />{k}{typeof v === 'string' && v !== 'off' ? ': ' + v : ''}
            </span>
          ))}
        </div>
      )}

      {/* Connection / routing — one compact line, full paths on hover */}
      <p className="muted tiny" style={{ marginTop: 12 }}>
        <span className="dot ok" /> <code title={status.upstream}>{hostOf(status.upstream)}</code>
        {' · '}{status.providers?.default || 'env fallback'}
        {' · '}{prettySurface(status.tool_surface)}
        {' · '}<code title={status.config_path}>{String(status.config_path || '').split('/').pop()}</code>
      </p>

      <details style={{ marginTop: 8 }}>
        <summary className="muted tiny" style={{ cursor: 'pointer' }}>Recent activity</summary>
        {events.length === 0 ? (
          <p className="muted tiny">No edits yet. Route an IDE chat through the gateway to see techniques fire.</p>
        ) : (
          <ul className="timeline">
            {events.map((e: any, i: number) => (
              <li key={i}>
                <span className={'t' + (e.type === 'notice' ? ' ' + (e.level === 'error' ? 'error' : 'warn') : '')}>{e.type === 'notice' ? (e.step || 'notice') : e.type}</span>
                <span className="muted tiny">
                  {e.type === 'notice' ? e.message : ''}
                  {e.freed_tokens ? `freed ~${e.freed_tokens} tok` : ''}
                  {e.cleared ? ` · cleared ${e.cleared}` : ''}
                  {e.removed ? ` · removed ${e.removed}` : ''}
                  {e.rasterised ? ` · rasterised ${e.rasterised}` : ''}
                  {e.compacted ? ` · compacted ${e.compacted}` : ''}
                </span>
                <span className="muted tiny" style={{ marginLeft: 'auto' }}>{e.ts ? rel(e.ts) : ''}</span>
              </li>
            ))}
          </ul>
        )}
      </details>
      <p style={{ marginTop: 12 }}><button className="btn sec sm" onClick={onRefresh}>Refresh</button></p>
    </div>
  );
}

// ── Chats (per-chat context windows) ──────────────────────────────────
function profileLabel(w: any): string {
  if (w && w.profile_source === 'preset') return w.profile_name || 'preset';
  if (w && w.profile_source === 'body') return 'custom';
  return 'default';
}

// ── Savings ────────────────────────────────────────────────────────────────
// The receipts: what ACM actually removed from your context, aggregated from the
// freed_tokens every technique reports. Per-chat and all-time, surviving restarts.
const TECH_LABEL: Record<string, string> = {
  visual_method: 'Visual method',
  tool_result_trimming: 'Tool trimming',
  image_eviction: 'Image eviction',
  summarization: 'Summarization',
  sliding_window: 'Sliding window',
};

function Savings() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('savings').then((d: any) => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [n]);

  // Every turn frees more tokens — keep the dashboard live.
  useAcmEvents(useCallback(() => reload(), [reload]));

  const resetAll = async () => {
    await rpc('savingsReset', {}).catch(() => {});
    reload();
  };

  if (loading) return <p className="muted tiny">Loading savings…</p>;
  const d = data || {};
  const total = Number(d.total_freed_tokens || 0);
  const rows: any[] = d.conversations || [];
  const byTech: Record<string, number> = d.by_technique || {};
  const cost = Number(d.total_cost_saved || 0);
  const maxRow = rows.reduce((m, r) => Math.max(m, Number(r.freed_tokens || 0)), 0) || 1;

  if (total === 0) {
    return (
      <div>
        <div className="card" style={{ padding: 14 }}>
          <div className="muted tiny">Tokens saved so far</div>
          <div style={{ fontSize: 30, fontWeight: 700, marginTop: 4 }}>0</div>
          <p className="muted tiny" style={{ marginTop: 8 }}>
            No savings recorded yet. As techniques trim, evict, and summarise your
            chats, the tokens they remove are tallied here — per chat and all-time.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Headline: the one number that says what ACM bought you */}
      <div className="card" style={{ padding: 14 }}>
        <div className="muted tiny">Tokens saved (all-time)</div>
        <div style={{ fontSize: 30, fontWeight: 700, marginTop: 4 }}>{fmtTok(total)}</div>
        <div className="muted tiny" style={{ marginTop: 4 }}>
          across {d.total_turns || 0} turn{d.total_turns === 1 ? '' : 's'}
          {cost > 0 ? ` · ~$${cost.toFixed(2)} saved` : ''}
        </div>
      </div>

      {/* Where the savings came from */}
      <h3 className="sec">By technique</h3>
      <div className="chips">
        {Object.keys(byTech).length === 0 && <span className="muted tiny">—</span>}
        {Object.entries(byTech)
          .sort((a, b) => Number(b[1]) - Number(a[1]))
          .map(([k, v]) => (
            <span key={k} className="chip on">
              {TECH_LABEL[k] || k} · {fmtTok(Number(v))}
            </span>
          ))}
      </div>

      {/* Per-chat leaderboard */}
      <h3 className="sec">By chat</h3>
      <ul className="timeline">
        {rows.map((r: any) => {
          const freed = Number(r.freed_tokens || 0);
          const pct = Math.round((freed / maxRow) * 100);
          return (
            <li key={r.conversation}>
              <span
                className="t"
                style={{ cursor: 'pointer' }}
                title={r.conversation}
                onClick={() => openChat(r.conversation)}
              >
                {r.title && r.title !== r.conversation ? r.title : shortId(r.conversation)}
              </span>
              <div style={{ height: 6, borderRadius: 4, overflow: 'hidden', background: 'rgba(127,127,127,0.18)', margin: '4px 0' }}>
                <div style={{ width: pct + '%', height: '100%', background: 'var(--accent)' }} />
              </div>
              <span className="muted tiny">
                {fmtTok(freed)} tok · {r.turns} turn{r.turns === 1 ? '' : 's'}
                {r.cost_saved > 0 ? ` · ~$${Number(r.cost_saved).toFixed(2)}` : ''}
                {r.last_ts ? ` · ${rel(r.last_ts)}` : ''}
              </span>
            </li>
          );
        })}
      </ul>

      <p style={{ marginTop: 12 }}>
        <button className="btn ghost sm" onClick={resetAll}>Reset savings</button>
      </p>
    </div>
  );
}

function Chats() {
  const [wins, setWins] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [n, reload] = useReload();
  // Live "generating now" set, kept off the poll path so the green dot flips the
  // instant the gateway announces it — seeded from each window's `running` flag
  // (covers a chat already mid-turn when this view mounts).
  const [running, setRunning] = useState<Record<string, boolean>>({});

  useEffect(() => {
    rpc('contextWindows', { project: projectRoot }).then((d: any) => {
      const w = d.windows || [];
      setWins(w);
      setRunning((prev) => {
        const next = { ...prev };
        for (const c of w) if (c.running) next[c.id] = true;
        return next;
      });
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [n]);

  // Realtime: a `running` event flips one chat's dot without a reload; any other
  // turn/window change refreshes the list (live titles, token counts, new chats).
  useAcmEvents(useCallback((e: AcmEvent) => {
    if (e.type === 'running' && e.conv) {
      setRunning((prev) => ({ ...prev, [e.conv as string]: !!e.running }));
      return;
    }
    reload();
  }, [reload]));

  const del = async (e: any, id: string) => {
    e.stopPropagation();
    const ok = await rpc('confirm', {
      message: 'Delete this context window and all its ACM state (drop-list, summaries)? ' +
        'The chat in your IDE is unaffected.',
    });
    if (!ok) return;
    setBusy(true);
    rpc('deleteWindow', { conv: id }).then(reload).finally(() => setBusy(false));
  };

  const clearAll = async () => {
    const ok = await rpc('confirm', {
      message: 'Clear ALL chats and captured state (context windows, drop-lists, summaries)? ' +
        'Provider config and memory are unaffected. This cannot be undone.',
    });
    if (!ok) return;
    setBusy(true);
    rpc('resetWindows', {}).then(reload).finally(() => setBusy(false));
  };

  if (loading) return <Loading />;
  if (wins.length === 0)
    return (
      <div className="empty">
        <p>No chats in this project yet.</p>
        <p className="tiny">Each Claude Code chat in this project becomes its own context window — with
          its own techniques. Point your IDE's model endpoint at the gateway and chat, then come back.</p>
        <button className="btn sec sm" onClick={reload}>Refresh</button>
      </div>
    );

  return (
    <div>
      <div className="row" style={{ alignItems: 'baseline', justifyContent: 'space-between' }}>
        <h3 className="sec" style={{ margin: 0 }}>Chats <span className="muted tiny">— this project · click to open</span></h3>
        <span className="row" style={{ gap: 6 }}>
          <button className="btn sec sm" onClick={reload}>Refresh</button>
          <button className="btn sec sm ghost" disabled={busy} onClick={clearAll}
            title="Clear all chats and captured state">Clear all</button>
        </span>
      </div>
      <p className="muted tiny">Each chat is its own context window with its own techniques. Open one to
        see exactly what's sent to the model and tune that chat's settings.</p>
      <div className="conv-list">
        {wins.map((c) => {
          const st = chatState(c, !!running[c.id]);
          const dotTitle = st === 'running'
            ? 'Active — generating a response now'
            : st === 'active'
            ? 'Active — used recently, idle now'
            : 'Idle — no recent activity';
          return (
          <div
            key={c.id}
            className="conv"
            onClick={() => openChat(c.id)}
            title="Open chat detail"
            // Per-session accent: a tinted left rail so different sessions read apart.
            style={{ borderLeft: `3px solid ${sessionColor(c.id)}` }}
          >
            <span className={`sdot ${st}`} title={dotTitle} aria-label={dotTitle} />
            <span className="id" style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.title || 'Untitled chat'}
              </span>
              <span className="muted tiny" style={{ fontFamily: 'var(--vscode-editor-font-family, monospace)', color: sessionColor(c.id) }} title={c.id}>{shortId(c.id)}</span>
            </span>
            <span className="meta">
              <span className="badge" title="Active technique profile for this chat">{profileLabel(c)}</span>
              <div className="count-badge">
                {fmtTok(c.tokens || 0)} tok · {c.messages} msg{c.dropped ? ` · ${c.dropped} removed` : ''}
              </div>
              <div className="row" style={{ gap: 6, alignItems: 'center', justifyContent: 'flex-end' }}>
                <span>{rel(c.last_seen)}</span>
                <button className="btn sm ghost" disabled={busy} title="Delete this context window"
                  onClick={(e) => del(e, c.id)}>✕</button>
              </div>
            </span>
          </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Preview (dry-run the pipeline on the next request) ───────────────────────
// Shows what the pipeline WOULD do to the current context before anything is
// sent: per-message kept / changed / removed / added, and the token delta. Free
// and deterministic — the paid summariser call is skipped (reported as pending).
const STATUS_LABEL: Record<string, string> = {
  kept: 'kept', changed: 'trimmed', removed: 'removed', added: 'added',
};

// One-click reversal of the last manual edit (drop / drop-many / restore /
// summarize) on this chat. The gateway keeps a session undo stack; we just show
// what's on top and pop it. Hidden entirely when there's nothing to undo.
function UndoBar({ conv }: { conv: string }) {
  const [top, setTop] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('undoStatus', { conv }).then((d: any) => setTop(d?.top || null)).catch(() => setTop(null));
  }, [conv, n]);
  // Any drop/restore/summarize elsewhere in the UI changes the stack.
  useAcmEvents(useCallback(() => reload(), [reload]));

  if (!top) return null;

  const doUndo = () => {
    setBusy(true);
    rpc('undo', { conv }).then(() => reload()).finally(() => setBusy(false));
  };

  return (
    <div className="row" style={{ alignItems: 'center', gap: 8, marginBottom: 8 }}>
      <button className="btn sm" disabled={busy} onClick={doUndo}>↩ Undo</button>
      <span className="muted tiny">{top.label}{top.depth > 1 ? ` · ${top.depth} steps back` : ''}</span>
    </div>
  );
}

function Preview({ conv }: { conv: string }) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [n, reload] = useReload();

  useEffect(() => {
    setLoading(true);
    rpc('preview', { conv }).then((d: any) => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [conv, n]);

  useAcmEvents(useCallback(() => reload(), [reload]));

  if (loading) return <p className="muted tiny">Computing preview…</p>;
  const d = data || {};
  if (!d.available) {
    return <p className="muted tiny">{d.reason || 'Preview unavailable.'}</p>;
  }

  const before = Number(d.before_tokens || 0);
  const after = Number(d.after_tokens || 0);
  const freed = Number(d.freed_tokens || 0);
  const rows: any[] = d.rows || [];
  const changed = rows.filter((r) => r.status !== 'kept');

  return (
    <div>
      <div className="card" style={{ padding: 12 }}>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span className="muted tiny">If sent now</span>
          <button className="btn ghost sm" onClick={reload}>Refresh</button>
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
          <span style={{ fontSize: 20, fontWeight: 700 }}>{fmtTok(after)}</span>
          <span className="muted tiny">
            tokens {freed > 0 ? `(down from ${fmtTok(before)}, −${fmtTok(freed)})` : '(no change)'}
          </span>
        </div>
        <div className="muted tiny" style={{ marginTop: 2 }}>
          {d.before_messages} → {d.after_messages} messages
          {d.summarization_pending ? ' · summarization will run live (needs a model call)' : ''}
        </div>
      </div>

      {changed.length === 0 ? (
        <p className="muted tiny">No mechanical changes — the context is already within limits.</p>
      ) : (
        <ul className="timeline">
          {changed.map((r: any, i: number) => (
            <li key={r.fp || i}>
              <span className={'t' + (r.status === 'removed' ? ' error' : r.status === 'added' ? '' : ' warn')}>
                {r.role} · {STATUS_LABEL[r.status] || r.status}
              </span>
              <span className="muted tiny" style={{ display: 'block' }}>{r.preview || '—'}</span>
              <span className="muted tiny">
                {r.status === 'changed'
                  ? `${fmtTok(r.tokens)} → ${fmtTok(r.after_tokens || 0)} tok`
                  : `${fmtTok(r.tokens)} tok`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Conversation (the whole chat in real order, each message removable) ──
// One message block: a clear role header, the full text, and a Remove/Restore
// button. Long messages fold to the first lines but are NEVER merged with any
// other message — the point of this view is that each message stands alone so
// the user can read it and pull it out of the model's context.
// One collapsible chunk of text with its own sub-label — used to render the
// individual parts a user turn splits into (your prompt vs. injected context).
// How much of a message to show before collapsing. Messages default to this
// short preview with a "Show full message" toggle; the button flips back to
// "Show less". Kept small so the conversation reads as a scannable list.
const PREVIEW_CHARS = 280;

function ConvPart({ part }: { part: CtxPart }) {
  const long = part.text.length > PREVIEW_CHARS;
  // Default to the short preview ("show less") — expand on demand.
  const [open, setOpen] = useState(false);
  const shown = open ? part.text : part.text.slice(0, PREVIEW_CHARS);
  const isPrompt = part.kind === 'prompt';
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ gap: 6, alignItems: 'center', marginBottom: 2 }}>
        <span className={'badge ' + (isPrompt ? 'user' : 'context')}>{part.label}</span>
        {!isPrompt && <span className="muted tiny">injected context</span>}
      </div>
      <div className="text" style={{ opacity: isPrompt ? 1 : 0.75 }}>
        {shown || <span className="muted">(empty)</span>}{!open && long ? '…' : ''}
      </div>
      {long && (
        <button className="btn ghost sm" style={{ marginTop: 2 }} onClick={() => setOpen((v) => !v)}>
          {open ? 'Show less' : 'Show full'}
        </button>
      )}
    </div>
  );
}

// Inline image render for a message that carries image block(s) — a tool
// screenshot or a visual-method rasterised page. Lazy-fetches the data URLs
// (they're big, so we never load them until the message is on screen) and shows
// them stacked under the message text, like an image message in a chat client.
function ConvImages({ m, conv }: { m: any; conv: string }) {
  const [imgs, setImgs] = useState<string[] | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    let alive = true;
    rpc('messageImages', { fp: m.fp, conv })
      .then((d: any) => { if (alive) { setImgs(d.images || []); if (d.error) setErr(d.error); } })
      .catch((e: any) => { if (alive) setErr(e.message || 'failed'); });
    return () => { alive = false; };
  }, [m.fp, conv]);

  if (err) return <div className="muted tiny" style={{ marginTop: 6 }}>Couldn't load image: {err}</div>;
  if (imgs === null) return <div className="muted tiny" style={{ marginTop: 6 }}><span className="spin" /> loading image…</div>;
  if (imgs.length === 0) return null;
  return (
    <div className="conv-imgs">
      {imgs.map((src, i) => (
        <img key={i} src={src} alt={`message image ${i + 1}`} loading="lazy" />
      ))}
    </div>
  );
}

function ConvMsg({ m, conv, onAct }: { m: any; conv: string; onAct: (method: string, fp: string) => void }) {
  const r = classify(m);
  const text = String(m.text ?? m.preview ?? '');

  // User-role turns can bundle your prompt with injected context — split them so
  // each part is labeled. Assistant/tool/system render as a single block.
  const parts = r.cls === 'user' || r.cls === 'context' ? splitUserParts(text) : null;
  const hasPrompt = !!parts && parts.some((p) => p.kind === 'prompt');
  const hasContext = !!parts && parts.some((p) => p.kind === 'context');

  const HEADER: Record<string, string> = {
    assistant: 'Assistant message',
    tool: 'Tool result',
    system: 'System message',
  };
  // Header reflects what the message actually contains once split.
  const header = parts
    ? hasPrompt && hasContext
      ? 'User message + context'
      : hasPrompt
        ? 'User message'
        : 'Injected context'
    : HEADER[r.cls] || r.label;
  // A message that's pure injected context reads as context (brown rail).
  const cls = parts && !hasPrompt && hasContext ? 'context' : r.cls;

  const long = !parts && text.length > PREVIEW_CHARS;
  // Collapsed by default: show a short preview with a "Show full message" toggle.
  const [open, setOpen] = useState(false);
  const shown = open ? text : text.slice(0, PREVIEW_CHARS);

  return (
    <div className={'msg ' + cls + (m.dropped ? ' dropped' : '')}>
      <div className="rail" />
      <div className="content">
        <div className="head">
          <span className={'badge ' + cls}>{header}</span>
          {typeof m.tokens === 'number' && m.tokens > 0 && (
            <span className="muted tiny">≈{fmtTok(m.tokens)} tok</span>
          )}
          {m.dropped && <span className="muted tiny" style={{ color: 'var(--bad, #e06c6c)' }}>removed</span>}
          <span className="act">
            {m.dropped
              ? <button className="btn ghost sm" onClick={() => onAct('restoreMessage', m.fp)}>Restore</button>
              : <button className="btn sm" onClick={() => { if (confirmDrop()) onAct('dropMessage', m.fp); }}>Remove</button>}
          </span>
        </div>
        {parts ? (
          <>
            {parts.map((p, i) => <ConvPart key={i} part={p} />)}
            {hasContext && (
              <p className="muted tiny" style={{ marginTop: 6 }}>
                Removing takes out the whole message (prompt + context together) — that's how Claude Code sends it.
              </p>
            )}
          </>
        ) : (
          <>
            <div className="text">{shown || <span className="muted">(empty)</span>}{!open && long ? '…' : ''}</div>
            {long && (
              <button className="btn ghost sm" style={{ marginTop: 4 }} onClick={() => setOpen((v) => !v)}>
                {open ? 'Show less' : 'Show full message'}
              </button>
            )}
          </>
        )}
        {m.has_image && <ConvImages m={m} conv={conv} />}
      </div>
    </div>
  );
}

// A collapsible turn for the full-text Conversation view: the user prompt (or a
// "context / system" summary) sits at the top with the turn's token total;
// expand to read and remove the individual messages, each rendered with the
// same split-aware ConvMsg block used in the flat view.
function ConvTurnGroup({ turn, conv, onAct }: { turn: ChatTurn; conv: string; onAct: (m: string, fp: string) => void }) {
  const [open, setOpen] = useState(false);
  const total =
    (turn.user ? (turn.user.tokens || 0) : 0) +
    turn.children.reduce((a, m) => a + (m.tokens || 0), 0);
  const count = turn.children.length + (turn.user ? 1 : 0);
  const isContextGroup = !turn.user;
  const badgeCls = isContextGroup ? 'context' : 'user';
  const badgeLabel = isContextGroup ? 'Context' : 'User';
  // Prefer the real prompt (first prompt part) so injected context never masks
  // what you actually typed; fall back to the raw text/preview.
  const headerText = turn.user
    ? (splitUserParts(String(turn.user.text ?? turn.user.preview ?? '')).find((p) => p.kind === 'prompt')?.text
        || String(turn.user.text ?? turn.user.preview ?? '(empty)'))
    : `${turn.children.length} context / system message${turn.children.length === 1 ? '' : 's'}`;
  return (
    <div className="turn" style={{ borderBottom: '1px solid var(--vscode-panel-border)', paddingBottom: 6, marginBottom: 6 }}>
      <div className="row" onClick={() => setOpen((v) => !v)} style={{ cursor: 'pointer', gap: 6 }}>
        <span className="muted">{open ? '▾' : '▸'}</span>
        <span className={'badge ' + badgeCls}>{badgeLabel}</span>
        <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, fontSize: 13 }}>{headerText}</span>
        <span className="muted tiny" style={{ whiteSpace: 'nowrap' }}>{count} msg</span>
        <span className="muted tiny" style={{ whiteSpace: 'nowrap' }}>≈{fmtTok(total)} tok</span>
      </div>
      {open && (
        <div style={{ marginTop: 6 }}>
          {turn.user && <ConvMsg m={turn.user} conv={conv} onAct={onAct} />}
          {turn.children.map((m, i) => <ConvMsg key={m.fp ?? i} m={m} conv={conv} onAct={onAct} />)}
        </div>
      )}
    </div>
  );
}

// The full transcript for one chat, in the exact order it happened. Fetches the
// complete message text up front (full=1) so every message renders in place;
// Remove tombstones it on the gateway so it's stripped from every future turn.
// Defaults to the grouped (per-turn) view; flip to Raw for the flat list.
export function Conversation({ conv }: { conv: string }) {
  const [msgs, setMsgs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [grouped, setGrouped] = useState(true);
  const [n, reload] = useReload();

  const load = useCallback(() => {
    rpc('messages', { conv, full: true })
      .then((d: any) => { setMsgs(d.messages || []); })
      .catch(() => setMsgs([]))
      .finally(() => setLoading(false));
  }, [conv]);
  useEffect(() => { setLoading(true); load(); }, [load, n]);
  useAcmEvents(useCallback((e: AcmEvent) => { if (!e.conv || e.conv === conv) reload(); }, [conv, reload]));

  const act = (method: string, fp: string) => rpc(method, { fp, conv }).then(reload);

  if (loading) return <p className="muted tiny">Loading conversation…</p>;
  if (msgs.length === 0) {
    return (
      <div className="empty">
        <p>No messages recorded for this chat yet.</p>
        <p className="tiny">Send a message through the gateway and the full conversation will appear here.</p>
      </div>
    );
  }
  const kept = msgs.filter((m) => !m.dropped).length;
  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <span className="muted tiny">
          {msgs.length} message{msgs.length === 1 ? '' : 's'} · {kept} in context · {msgs.length - kept} removed
        </span>
        <div className="row" style={{ gap: 4 }}>
          <button className={'btn sm ' + (grouped ? 'ghost' : '')} onClick={() => setGrouped(false)}>Raw</button>
          <button className={'btn sm ' + (grouped ? '' : 'ghost')} onClick={() => setGrouped(true)}>Grouped</button>
          <button className="btn ghost sm" onClick={reload}>Refresh</button>
        </div>
      </div>
      <p className="muted tiny" style={{ marginTop: 4 }}>
        The whole conversation in order. Remove drops a message from the model on every future
        turn — it stays visible here (struck through) so you can restore it.
      </p>
      <div className="card">
        {grouped
          ? groupTurns(msgs).map((t, i) => (
              <ConvTurnGroup key={t.user?.fp ?? 'turn-' + i} turn={t} conv={conv} onAct={act} />
            ))
          : msgs.map((m, i) => <ConvMsg key={m.fp ?? i} m={m} conv={conv} onAct={act} />)}
      </div>
    </div>
  );
}

// ── Chat detail (two columns: context window | per-chat settings) ──────
export function ChatDetail({ conv }: { conv: string }) {
  const [theme] = useState<Theme>(() => (getState().theme as Theme) || 'auto');
  const [win, setWin] = useState<any>(null);
  const [presets, setPresets] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('getContextWindow', { conv }).then(setWin).catch(() => setWin(null));
  }, [conv, n]);
  useEffect(() => {
    rpc('getProfile').then((d: any) => setPresets(d.presets || [])).catch(() => {});
  }, []);

  const setProfile = (value: string) => {
    setBusy(true);
    const params = value === '' ? { conv, clear: true } : { conv, name: value };
    rpc('setWindowProfile', params).then(reload).finally(() => setBusy(false));
  };

  const source = win?.profile_source || 'global';
  return (
    <div className="acm" data-theme={theme}>
      <header className="hd">
        <svg className="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          <path d="m3 12 9 4.5L21 12M3 16.5l9 4.5 9-4.5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
        <div style={{ minWidth: 0 }}>
          <div className="title">{win?.title || 'Untitled chat'}</div>
          <div className="sub" style={{ fontFamily: 'var(--vscode-editor-font-family, monospace)' }} title={conv}>{shortId(conv)}</div>
        </div>
      </header>
      <main className="body">
        <div className="one-col">
          <section className="col">
            <h3 className="sec">Conversation <span className="muted tiny">— every message, in order · remove any</span></h3>
            <UndoBar conv={conv} />
            <Conversation conv={conv} />
            <h3 className="sec" style={{ marginTop: 16 }}>Settings <span className="muted tiny">— this chat only</span></h3>
            <div className="card">
              <div className="row" style={{ alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <strong className="tiny">Profile</strong>
                <select disabled={busy}
                  value={source === 'preset' ? (win?.profile_name || '') : (source === 'body' ? '__custom__' : '')}
                  onChange={(e) => setProfile(e.target.value === '__custom__' ? '' : e.target.value)}>
                  <option value="">Global default</option>
                  {presets.map((p: any) => <option key={p.name} value={p.name}>{p.name}</option>)}
                  {source === 'body' && <option value="__custom__" disabled>custom (inline)</option>}
                </select>
                <span className="muted tiny" style={{ marginLeft: 'auto' }}>
                  {source === 'global' ? 'inheriting global default' : source === 'preset' ? `preset: ${win?.profile_name}` : 'custom techniques'}
                </span>
              </div>
            </div>

            <h4 className="sec" style={{ marginTop: 16 }}>Techniques</h4>
            <Techniques conv={conv} onChanged={reload} />

            <h4 className="sec" style={{ marginTop: 16 }}>Cleanup</h4>
            <Cleanup conv={conv} />
          </section>
        </div>
      </main>
    </div>
  );
}

// Show the "this can't be undone" confirm only once per session, then remove
// straight away on later clicks (better UX for bulk cleanup).
let skipDropConfirm = false;
function confirmDrop(): boolean {
  if (skipDropConfirm) return true;
  let ok = true;
  try {
    ok = window.confirm(
      "Remove this message from the model's context on every future turn? " +
        "You won't be asked again this session.",
    );
  } catch {
    ok = true; // confirm unavailable in this webview — proceed
  }
  if (ok) skipDropConfirm = true;
  return ok;
}

function fmtTok(n: number): string {
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(n);
}

// Group the flat transcript into conversation turns: a user message starts a
// turn; the assistant/tool messages it produced are its children.
type ChatTurn = { user: any | null; children: any[] };
function groupTurns(msgs: any[]): ChatTurn[] {
  const groups: ChatTurn[] = [];
  let cur: ChatTurn | null = null;
  for (const m of msgs) {
    // Only a REAL user prompt opens a turn. System + injected-context messages
    // (which Claude Code sends as user-role) attach to the surrounding group, so
    // the noise collapses and your actual prompts stand out.
    const startsTurn = classify(m).cls === 'user';
    if (startsTurn) {
      if (cur) groups.push(cur);
      cur = { user: m, children: [] };
    } else {
      if (!cur) cur = { user: null, children: [] };
      cur.children.push(m);
    }
  }
  if (cur) groups.push(cur);
  return groups;
}

// ── Cleanup (task-aware relevance suggestions) ─────────────────────────
function Cleanup({ conv: fixedConv }: { conv?: string } = {}) {
  const [convs, setConvs] = useState<any[]>([]);
  const [sel, setSel] = useState<string>(fixedConv || '');
  const [sugs, setSugs] = useState<any[] | null>(null);
  const [info, setInfo] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (fixedConv) { setSel(fixedConv); return; }
    rpc('conversations').then((d: any) => {
      const list = d.conversations || [];
      setConvs(list);
      setSel((cur) => cur || (list[0] ? list[0].key : ''));
    }).catch(() => {});
  }, [fixedConv]);

  const analyze = async () => {
    setBusy(true); setErr(''); setSugs(null); setInfo(null);
    try {
      const d: any = await rpc('relevanceSuggest', { conv: sel });
      if (d.error) setErr(d.error);
      setSugs(d.suggestions || []);
      setInfo(d.info || null);
      if (d.conversation && d.conversation !== sel) setSel(d.conversation);
    } catch (e: any) {
      setErr(e.message || 'failed');
    } finally {
      setBusy(false);
    }
  };

  // Apply a decision to one suggestion and log the user's choice for training.
  const decide = async (s: any, action: 'accept_drop' | 'reject' | 'restore') => {
    const conv = sel;
    try {
      if (action === 'accept_drop') {
        await rpc('dropMany', { fps: s.member_fps, conv });
      } else if (action === 'restore') {
        for (const fp of s.member_fps) await rpc('restoreMessage', { fp, conv });
      }
      await rpc('relevanceFeedback', {
        payload: {
          conv,
          episode_id: s.episode_id,
          title: s.title,
          shown_label: s.label,
          user_action: action,
          final_label: action === 'accept_drop' ? s.label : 'KEEP',
          score: s.score,
          source: s.source,
          tokens: s.freed_tokens,
        },
      });
    } catch (e: any) {
      setErr(e.message || 'action failed');
      return;
    }
    setSugs((cur) =>
      (cur || []).map((x) =>
        x.episode_id === s.episode_id ? { ...x, dropped: action === 'accept_drop' } : x,
      ),
    );
  };

  // Replace an episode with a short summary (saves tokens, keeps the gist).
  const summarize = async (s: any) => {
    const conv = sel;
    try {
      const d: any = await rpc('relevanceSummarize', {
        member_fps: s.member_fps,
        conv,
        title: s.title,
      });
      if (d?.error) { setErr(d.error); return; }
      await rpc('relevanceFeedback', {
        payload: {
          conv,
          episode_id: s.episode_id,
          title: s.title,
          shown_label: s.label,
          user_action: 'accept_summarize',
          final_label: 'SUMMARIZE',
          score: s.score,
          source: s.source,
          tokens: s.freed_tokens,
        },
      });
    } catch (e: any) {
      setErr(e.message || 'summarize failed');
      return;
    }
    setSugs((cur) =>
      (cur || []).map((x) =>
        x.episode_id === s.episode_id ? { ...x, dropped: true, summarized: true } : x,
      ),
    );
  };

  const actionable = (sugs || []).filter((s) => s.label !== 'KEEP');
  const kept = (sugs || []).filter((s) => s.label === 'KEEP');

  return (
    <div>
      <p className="muted tiny">
        The auditor splits this conversation into episodes and suggests which finished or
        unrelated ones to remove. Nothing is removed until you click — and every choice is
        logged to improve the model.
      </p>
      <div className="row">
        {!fixedConv && (
          <select value={sel} onChange={(e) => setSel(e.target.value)} style={{ minWidth: 0, flex: 1 }}>
            {convs.length === 0 && <option value="">(no conversations seen yet)</option>}
            {convs.map((c) => (
              <option key={c.key} value={c.key}>{c.key} · {c.count} msg</option>
            ))}
          </select>
        )}
        <button className="btn" onClick={analyze} disabled={busy || !sel}>
          {busy ? <><span className="spin" /> Analyzing…</> : 'Analyze relevance'}
        </button>
      </div>

      {err && <div className="banner" style={{ marginTop: 10 }}>{err}</div>}

      {info && (
        <h3 className="sec">
          {info.candidates || 0} candidate{(info.candidates || 0) === 1 ? '' : 's'} ·
          {' '}{info.drop || 0} drop · {info.summarize || 0} summarize ·
          {' '}~{info.potential_freed_tokens || 0} tokens recoverable
        </h3>
      )}

      {actionable.map((s) => {
        const sw = statusWord(s);
        const canSummarize = s.label === 'SUMMARIZE';
        return (
          <div className="card" key={s.episode_id} style={{ opacity: s.dropped ? 0.55 : 1, padding: '8px 10px' }}>
            <div className="row">
              <span title={s.reason} style={{ background: sw.bg, color: sw.fg, padding: '1px 6px', borderRadius: 5, fontWeight: 600, fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.4 }}>
                {sw.w}
              </span>
              <strong title={s.title + '\n' + s.reason} style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 }}>{s.title}</strong>
              <span className="muted tiny" style={{ marginLeft: 'auto', whiteSpace: 'nowrap' }}>~{s.freed_tokens} tok</span>
            </div>
            <div className="row" style={{ marginTop: 6 }}>
              {s.dropped
                ? <span className="muted tiny">{s.summarized ? 'summarized' : 'removed'} · <button className="btn ghost sm" onClick={() => decide(s, 'restore')}>Restore</button></span>
                : <>
                    {canSummarize && <button className="btn ghost sm" onClick={() => summarize(s)}>Summarize</button>}
                    <button className="btn sm" onClick={() => decide(s, 'accept_drop')}>Remove</button>
                    <button className="btn ghost sm" onClick={() => decide(s, 'reject')}>Keep</button>
                  </>}
              <span className="muted tiny" style={{ marginLeft: 'auto' }}><code>{s.source}</code> · {s.member_fps.length} msg</span>
            </div>
          </div>
        );
      })}

      {sugs && actionable.length === 0 && !err && (
        <p className="muted tiny" style={{ marginTop: 10 }}>Nothing to remove — every episode looks relevant to the current task.</p>
      )}

      {kept.length > 0 && (
        <>
          <h3 className="sec">Kept ({kept.length})</h3>
          {kept.map((s) => (
            <div className="card tiny" key={s.episode_id}>
              <span className="muted">KEEP</span> · {s.title}
              <span className="muted"> — {s.reason}</span>
            </div>
          ))}
        </>
      )}

    </div>
  );
}

// ── Techniques ─────────────────────────────────────────────────────────
function Techniques({ conv, onChanged }: { conv?: string; onChanged?: () => void } = {}) {
  const perChat = !!conv;
  const [prof, setProf] = useState<any>(null);
  const [vm, setVm] = useState<any>(null); // visual method is a global axis — global tab only
  const [presets, setPresets] = useState<any[]>([]);
  const [msg, setMsg] = useState('');
  const [n, reload] = useReload();
  useEffect(() => {
    if (perChat) {
      rpc('getContextWindow', { conv }).then((w: any) => { setProf(w.profile); setVm(null); });
    } else {
      rpc('getProfile').then((p: any) => {
        setProf(p.active);
        setPresets(p.presets || []);
        setVm(p.visual_method || { enabled: false, trigger_tokens: 500, only_tools: [], exclude_tools: [] });
      });
    }
  }, [n, conv]);
  if (!prof) return <Loading />;
  const applyPreset = async (name: string) => {
    setMsg('applying ' + name + '…');
    try { await rpc('setPreset', { name }); setMsg(name + ' applied ✓'); reload(); }
    catch (e: any) { setMsg('Error: ' + e.message); }
  };
  const cm = prof.context_management;
  const setCM = (key: string, field: string, value: any) => { const x = clone(prof); x.context_management[key][field] = value; setProf(x); };
  const save = async () => {
    setMsg('saving…');
    try {
      if (perChat) { await rpc('setWindowProfile', { conv, body: prof }); onChanged?.(); }
      else { await rpc('setProfileBody', { body: prof, visual_method: vm }); }
      setMsg('Saved ✓');
    }
    catch (e: any) { setMsg('Error: ' + e.message); }
  };
  return (
    <div>
      <p className="muted tiny">{perChat
        ? 'Toggle techniques for this chat only. Saved to this context window; the next turn uses them.'
        : 'Toggle techniques the gateway applies by default to every chat. Changes save to your config and take effect on the next request.'}</p>
      {!perChat && presets.length > 0 && (
        <details className="card" style={{ marginBottom: 10 }}>
          <summary><strong>Presets</strong> <span className="muted tiny">— apply a bundle for a use case (overwrites the toggles below)</span></summary>
          {presets.map((preset: any) => (
            <div className="row" key={preset.name} style={{ marginTop: 8 }}>
              <div>
                <strong>{preset.name}</strong>
                <div className="desc muted tiny">{preset.summary}</div>
              </div>
              <button className="btn sm" style={{ marginLeft: 'auto' }} onClick={() => applyPreset(preset.name)}>Apply</button>
            </div>
          ))}
        </details>
      )}
      {TECHS.map((t) => (
        <div className="card tech" key={t.key}>
          <Toggle on={!!cm[t.key].enabled} onChange={(v) => setCM(t.key, 'enabled', v)} />
          <div className="meta">
            <div className="name">{t.name}</div>
            <div className="desc">{t.desc}</div>
            {cm[t.key].enabled && t.params.length > 0 && (
              <div className="params">
                {t.params.map(([f, lbl]) => (
                  <label key={f} className="field">{lbl}
                    <input type="number" value={cm[t.key][f]} onChange={(e) => setCM(t.key, f, Number(e.target.value))} />
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}

      <div className="card tech">
        <span />
        <div className="meta">
          <div className="name">Image recall</div>
          <div className="desc">Manage tool screenshots: cache the settled prefix and/or evict old images to their text references.</div>
          <div className="params">
            <label className="field">Mode
              <select value={cm.image_recall.mode} onChange={(e) => setCM('image_recall', 'mode', e.target.value)}>
                <option value="off">off</option><option value="cache">cache</option>
                <option value="evict">evict</option><option value="cache_evict">cache + evict</option>
              </select>
            </label>
            <label className="field">Keep recent images
              <input type="number" value={cm.image_recall.keep_recent_images} onChange={(e) => setCM('image_recall', 'keep_recent_images', Number(e.target.value))} />
            </label>
          </div>
        </div>
      </div>

      {cm.relevance_pruning && (
        <div className="card tech">
          <Toggle on={!!cm.relevance_pruning.enabled} onChange={(v) => setCM('relevance_pruning', 'enabled', v)} />
          <div className="meta">
            <div className="name">Relevance cleanup</div>
            <div className="desc">Split the chat into episodes and suggest which finished/unrelated ones to remove. Suggest-only — review &amp; apply in Cleanup (open a chat).</div>
            {cm.relevance_pruning.enabled && (
              <div className="params">
                <label className="field">Engine
                  <select value={cm.relevance_pruning.mode} onChange={(e) => setCM('relevance_pruning', 'mode', e.target.value)}>
                    <option value="judge">judge (LLM)</option>
                    <option value="encoder">encoder (local)</option>
                    <option value="ensemble">ensemble (both)</option>
                  </select>
                </label>
                {cm.relevance_pruning.mode === 'ensemble' && (
                  <label className="field">On disagreement
                    <select value={cm.relevance_pruning.arbitration} onChange={(e) => setCM('relevance_pruning', 'arbitration', e.target.value)}>
                      <option value="safest">safest (keep)</option>
                      <option value="judge_wins">judge wins</option>
                      <option value="agreement_only">only if both agree</option>
                    </select>
                  </label>
                )}
                <label className="field">Keep recent episodes
                  <input type="number" value={cm.relevance_pruning.keep_recent} onChange={(e) => setCM('relevance_pruning', 'keep_recent', Number(e.target.value))} />
                </label>
              </div>
            )}
          </div>
        </div>
      )}

      {!perChat && vm && (
      <div className="card tech">
        <Toggle on={!!vm.enabled} onChange={(v) => setVm({ ...vm, enabled: v })} />
        <div className="meta">
          <div className="name">Visual method</div>
          <div className="desc">Render big tool outputs as an image the model reads, keeping URLs &amp; citations as text. Best for noisy, image-friendly outputs.</div>
          {vm.enabled && (
            <div className="params">
              <label className="field">Trigger (tokens)
                <input type="number" value={vm.trigger_tokens} onChange={(e) => setVm({ ...vm, trigger_tokens: Number(e.target.value) })} />
              </label>
            </div>
          )}
        </div>
      </div>
      )}

      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn" onClick={save}>Save changes</button>
        <button className="btn sec" onClick={reload}>Revert</button>
        <span className="toast">{msg}</span>
      </div>
    </div>
  );
}

// ── Providers ──────────────────────────────────────────────────────────
const EMPTY = { slug: '', type: 'openai', api_key: '', base_url: '', azure_endpoint: '', api_version: '', default: false };
function Providers() {
  const [data, setData] = useState<any>(null);
  const [form, setForm] = useState<any>(EMPTY);
  const [msg, setMsg] = useState('');
  const [n, reload] = useReload();
  useEffect(() => { rpc('providers').then(setData); }, [n]);
  if (!data) return <Loading />;
  const provs = data.providers || {};
  const save = async () => {
    if (!form.slug) { setMsg('Slug required'); return; }
    const cfg: any = { slug: form.slug, type: form.type, default: form.default };
    ['api_key', 'base_url'].forEach((k) => form[k] && (cfg[k] = form[k]));
    if (form.type === 'azure') { cfg.azure_endpoint = form.azure_endpoint; if (form.api_version) cfg.api_version = form.api_version; }
    setMsg('saving…');
    try { await rpc('upsertProvider', { cfg }); setMsg('Saved ✓'); setForm(EMPTY); reload(); }
    catch (e: any) { setMsg('Error: ' + e.message); }
  };
  return (
    <div>
      <p className="muted tiny">Route requests to any provider. OpenAI / OpenRouter / Google / Azure use the OpenAI surface; Anthropic is native. Bedrock needs AWS signing — route it via OpenRouter.</p>
      <h3 className="sec">Configured · default: {data.default || 'env fallback'}</h3>
      {Object.keys(provs).length === 0 ? <p className="muted tiny">None yet — using the env upstream.</p> :
        Object.entries(provs).map(([slug, c]: any) => (
          <div className="card" key={slug}>
            <div className="row">
              <strong>{data.default === slug ? '★ ' : ''}{slug}</strong>
              <span className="chip">{c.type}</span>
              <code className="tiny">{c.api_key || '—'}</code>
              <span style={{ marginLeft: 'auto' }} className="row">
                {data.default !== slug && <button className="btn ghost sm" onClick={() => rpc('setDefaultProvider', { slug }).then(reload)}>Make default</button>}
                <button className="btn ghost sm" onClick={() => rpc('deleteProvider', { slug }).then(reload)}>Delete</button>
              </span>
            </div>
          </div>
        ))}

      <h3 className="sec">Add / update</h3>
      <div className="card">
        <div className="row">
          <input type="text" placeholder="slug (e.g. openai)" value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
          <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            {['openai', 'openrouter', 'google', 'azure', 'anthropic', 'custom'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="row" style={{ marginTop: 6 }}>
          <input type="text" placeholder="api key" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
        </div>
        {form.type === 'azure' && (
          <div className="row" style={{ marginTop: 6 }}>
            <input type="text" placeholder="azure endpoint" value={form.azure_endpoint} onChange={(e) => setForm({ ...form, azure_endpoint: e.target.value })} />
            <input type="text" placeholder="api version" value={form.api_version} onChange={(e) => setForm({ ...form, api_version: e.target.value })} />
          </div>
        )}
        {form.type !== 'azure' && (
          <div className="row" style={{ marginTop: 6 }}>
            <input type="text" placeholder="base url (optional)" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
          </div>
        )}
        <div className="row" style={{ marginTop: 8 }}>
          <label className="row tiny"><input type="checkbox" checked={form.default} onChange={(e) => setForm({ ...form, default: e.target.checked })} /> make default</label>
          <button className="btn" onClick={save}>Save provider</button>
          <span className="toast">{msg}</span>
        </div>
      </div>
    </div>
  );
}

// ── Memory ─────────────────────────────────────────────────────────────
function Memory() {
  const [scope, setScope] = useState('user');
  const [items, setItems] = useState<string[]>([]);
  const [text, setText] = useState('');
  const [n, reload] = useReload();
  useEffect(() => { rpc('recall', { query: '', scope }).then((r: any) => setItems(r.items || [])); }, [n, scope]);
  return (
    <div>
      <p className="muted tiny">Notes the agent (or you) saved. <code>user</code> scope is shared across chats; <code>thread</code> is per-conversation.</p>
      <div className="row">
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          <option value="user">user</option><option value="thread">thread</option>
        </select>
        <button className="btn sec sm" onClick={reload}>Refresh</button>
        <button className="btn ghost sm" onClick={() => rpc('memoryClear', { scope }).then(reload)}>Clear scope</button>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <input type="text" placeholder="add a note…" value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) { rpc('remember', { text, scope }).then(() => { setText(''); reload(); }); } }} />
        <button className="btn" onClick={() => { if (text.trim()) rpc('remember', { text, scope }).then(() => { setText(''); reload(); }); }}>Add</button>
      </div>
      <h3 className="sec">{items.length} note{items.length === 1 ? '' : 's'}</h3>
      {items.length === 0 ? <p className="muted tiny">Empty.</p> :
        items.map((it, i) => <div className="card tiny" key={i}>{it}</div>)}
    </div>
  );
}

// Training data export (relevance feedback → encoder + judge trainer files).
// Every accept/reject/summarize on a pruning suggestion is logged; this turns
// that log into the two files the FYP trainers read. Read-only summary first
// (how much data exists, how often the user overrode the model), then export.
function Training() {
  const [sum, setSum] = useState<any>(null);
  const [incl, setIncl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [manifest, setManifest] = useState<any>(null);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('trainingSummary', { includeModelLabels: incl }).then(setSum).catch(() => setSum(null));
  }, [incl, n]);

  const doExport = () => {
    setBusy(true);
    rpc('trainingExport', { includeModelLabels: incl })
      .then((m: any) => { setManifest(m); reload(); })
      .finally(() => setBusy(false));
  };

  const s = sum || {};
  const counts = s.label_counts || {};
  const gold = Number(s.gold_examples || 0);
  const pct = Math.round(Number(s.override_rate || 0) * 100);

  return (
    <div>
      <p className="muted tiny">
        Turns your pruning feedback into training data — <code>encoder.jsonl</code> (relevance
        model) and <code>judge_dpo.jsonl</code> (preference pairs). Data comes from every
        KEEP/SUMMARIZE/DROP decision you make on suggestions.
      </p>

      {s.error ? (
        <p className="muted tiny">Couldn't read logs: {s.error}</p>
      ) : (
        <div className="card" style={{ padding: 12 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontSize: 20, fontWeight: 700 }}>{gold}</span>
            <span className="muted tiny">gold examples (your corrections)</span>
          </div>
          <div className="muted tiny" style={{ marginTop: 2 }}>
            {s.silver_examples || 0} silver · {s.judge_pairs || 0} DPO pairs · {pct}% of
            comparable episodes overrode the model
          </div>
          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
            {['KEEP', 'SUMMARIZE', 'DROP'].map((k) => (
              <span key={k} className="badge tiny">{k}: {counts[k] || 0}</span>
            ))}
          </div>
        </div>
      )}

      <label className="row tiny" style={{ gap: 6, marginTop: 10, alignItems: 'center' }}>
        <input type="checkbox" checked={incl} onChange={(e) => setIncl(e.target.checked)} />
        Include model's own labels as silver examples (cold-start, before enough corrections)
      </label>

      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn" disabled={busy || gold === 0} onClick={doExport}>
          {busy ? 'Exporting…' : 'Export training files'}
        </button>
        <button className="btn ghost sm" onClick={reload}>Refresh</button>
      </div>
      {gold === 0 && (
        <p className="muted tiny" style={{ marginTop: 6 }}>
          No corrections logged yet — accept or reject some pruning suggestions first.
        </p>
      )}

      {manifest && manifest.ok && (
        <div className="card tiny" style={{ marginTop: 10 }}>
          <div>Wrote {manifest.encoder_examples} encoder rows + {manifest.judge_pairs} DPO pairs to:</div>
          <div style={{ fontFamily: 'var(--vscode-editor-font-family, monospace)', marginTop: 4 }}>
            {manifest.encoder_path}<br />{manifest.judge_path}
          </div>
        </div>
      )}
      {manifest && manifest.error && (
        <p className="muted tiny" style={{ marginTop: 6 }}>Export failed: {manifest.error}</p>
      )}
    </div>
  );
}

// ── Context Window (the exact payload we forward to the model each call) ──
// The gateway snapshots this AFTER its technique pipeline runs (drops + trimming
// + summaries already applied), so it is literally "what the model sees on every
// call" — distinct from the Chats tab, which shows the incoming messages.
type CwCat = 'context' | 'tools' | 'skills' | 'user' | 'thinking' | 'assistant' | 'tool';
const CW_SECTIONS: { cat: CwCat; title: string }[] = [
  { cat: 'context', title: 'Context window' },
  { cat: 'tools', title: 'Available tools' },
  { cat: 'skills', title: 'Skills' },
  { cat: 'user', title: 'User message' },
  { cat: 'thinking', title: 'Thinking' },
  { cat: 'assistant', title: 'My response' },
  { cat: 'tool', title: 'Tool response' },
];
// Which role-colour badge each section/segment uses (reuses the .badge.* styles).
const CW_BADGE: Record<CwCat, string> = {
  context: 'context', tools: 'system', skills: 'system',
  user: 'user', thinking: 'assistant', assistant: 'assistant', tool: 'tool',
};

// Same ~4-chars/token estimate the HUD / Overview / gateway use, kept consistent.
const estTok = (s: string): number => (s && s.trim() ? Math.max(1, Math.ceil(s.length / 4)) : 0);

// Flatten provider-shaped content (string | block[]) to plain text/markdown.
function cwFlatten(content: any): string {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => {
        if (typeof b === 'string') return b;
        if (!b || typeof b !== 'object') return String(b ?? '');
        if (b.type === 'text') return b.text || '';
        if (b.type === 'thinking') return b.thinking || '';
        if (b.type === 'image' || b.type === 'image_url') return '_[image]_';
        if (b.type === 'tool_use') return '→ called `' + (b.name || 'tool') + '`' + (b.input ? ' `' + JSON.stringify(b.input) + '`' : '');
        if (b.type === 'tool_result') return cwFlatten(b.content);
        return b.text || '';
      })
      .filter(Boolean)
      .join('\n\n');
  }
  return String(content);
}

// Pull a "skills are available" listing out of context text (the header line —
// which may be followed by a blank line — plus the bullet list under it).
const CW_SKILLS_RE = /The following skills are available[^\n]*\n+(?:[ \t]*-[^\n]*\n?)+/;

type CwSeg = { cat: CwCat; label: string; text: string; tokens: number };
type CwRaw = { label: string; cls: string; text: string };
type CwAnalysis = {
  segments: CwSeg[];
  raw: CwRaw[];
  tools: { name: string; desc: string; tokens: number }[];
  catTokens: Record<string, number>;
  total: number;
};

// Normalise the captured wire body (either provider surface) into: categorised
// segments (Proper format / Token usage) + an ordered per-message list (Raw).
function cwAnalyze(data: any): CwAnalysis {
  const surface = (data && data.surface) || '';
  const msgs: any[] = data && Array.isArray(data.messages) ? data.messages : [];
  const segments: CwSeg[] = [];
  const raw: CwRaw[] = [];
  let skills = '';

  const harvestSkills = (text: string): string => {
    const m = text.match(CW_SKILLS_RE);
    if (m) { skills += (skills ? '\n\n' : '') + m[0].trim(); return text.replace(m[0], '').trim(); }
    return text;
  };
  const pushSeg = (cat: CwCat, label: string, text: string) => {
    if (text && text.trim()) segments.push({ cat, label, text, tokens: estTok(text) });
  };

  // Anthropic: the system prompt is a separate field, not a message.
  if (surface === 'anthropic' && data.system != null) {
    const sysFull = cwFlatten(data.system);
    pushSeg('context', 'System prompt', harvestSkills(sysFull));
    raw.push({ label: 'System', cls: 'context', text: sysFull });
  }

  for (const m of msgs) {
    const role = String((m && m.role) || '').toLowerCase();
    const full = cwFlatten(m && m.content);
    if (role === 'system') {
      pushSeg('context', 'System prompt', harvestSkills(full));
      raw.push({ label: 'System', cls: 'context', text: full });
    } else if (role === 'user' || role === 'human') {
      // Anthropic returns tool results on a user-role message — treat as tool.
      const isToolResult = Array.isArray(m.content) && m.content.some((b: any) => b && b.type === 'tool_result');
      if (isToolResult) {
        pushSeg('tool', 'Tool result', full);
        raw.push({ label: 'Tool', cls: 'tool', text: full });
      } else {
        const ctx = CONTEXT_RE.test(full);
        pushSeg(ctx ? 'context' : 'user', ctx ? 'Injected context' : 'User', ctx ? harvestSkills(full) : full);
        raw.push({ label: ctx ? 'Context' : 'User', cls: ctx ? 'context' : 'user', text: full });
      }
    } else if (role === 'assistant' || role === 'ai') {
      let thinking = '';
      let say = '';
      if (Array.isArray(m.content)) {
        for (const b of m.content) {
          if (!b || typeof b !== 'object') { say += String(b ?? ''); continue; }
          if (b.type === 'thinking') thinking += (b.thinking || '') + '\n';
          else if (b.type === 'text') say += (b.text || '') + '\n';
          else if (b.type === 'tool_use') say += '→ called `' + (b.name || 'tool') + '`' + (b.input ? ' `' + JSON.stringify(b.input) + '`' : '') + '\n';
        }
      } else {
        say = typeof m.content === 'string' ? m.content : cwFlatten(m.content);
      }
      // OpenAI tool calls live on the message, not in content blocks.
      const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
      for (const c of calls) {
        const fn = (c && c.function) || {};
        say += '→ called `' + (fn.name || (c && c.name) || 'tool') + '`' + (fn.arguments ? ' `' + fn.arguments + '`' : '') + '\n';
      }
      pushSeg('thinking', 'Thinking', thinking.trim());
      pushSeg('assistant', 'My response', say.trim());
      const rawText = [thinking.trim() && ('> 🧠 ' + thinking.trim().replace(/\n/g, '\n> ')), say.trim()].filter(Boolean).join('\n\n') || full;
      raw.push({ label: 'Assistant', cls: 'assistant', text: rawText });
    } else if (role === 'tool') {
      pushSeg('tool', 'Tool result', full);
      raw.push({ label: 'Tool', cls: 'tool', text: full });
    } else {
      pushSeg('context', role || 'Other', full);
      raw.push({ label: role || 'Other', cls: 'system', text: full });
    }
  }

  if (skills) pushSeg('skills', 'Skills', skills);

  const toolsArr: any[] = data && Array.isArray(data.tools) ? data.tools : [];
  const tools = toolsArr.map((t) => {
    const fn = (t && t.function) || t || {};
    return {
      name: fn.name || (t && t.name) || '(unnamed)',
      desc: fn.description || (t && t.description) || '',
      tokens: estTok(JSON.stringify(t || {})),
    };
  });

  const catTokens: Record<string, number> = {};
  for (const s of segments) catTokens[s.cat] = (catTokens[s.cat] || 0) + s.tokens;
  catTokens.tools = tools.reduce((acc, t) => acc + t.tokens, 0);
  const total = Object.values(catTokens).reduce((acc, n) => acc + n, 0);

  return { segments, raw, tools, catTokens, total };
}

function CwMd({ text }: { text: string }) {
  return <div className="cw-md"><ReactMarkdown>{text}</ReactMarkdown></div>;
}

// One collapsible message/segment block; long blocks (e.g. the system prompt)
// can be folded for navigation but default to fully expanded ("show fully").
function CwBlock({ label, cls, tokens, text }: { label: string; cls: string; tokens?: number; text: string }) {
  const [open, setOpen] = useState(true);
  const long = text.length > 1500;
  return (
    <div className={'msg ' + cls}>
      <div className="rail" />
      <div className="content">
        <div className="head">
          <span className={'badge ' + cls}>{label}</span>
          {typeof tokens === 'number' ? <span className="muted tiny">≈{fmtTok(tokens)} tok</span> : null}
          {long ? <button className="btn ghost sm act" onClick={() => setOpen((v) => !v)}>{open ? 'Collapse' : 'Expand'}</button> : null}
        </div>
        {open ? <CwMd text={text} /> : <div className="muted tiny">{text.slice(0, 160)}…</div>}
      </div>
    </div>
  );
}

function CwProper({ a }: { a: CwAnalysis }) {
  const sections = CW_SECTIONS.map((sec) => {
    if (sec.cat === 'tools') {
      if (!a.tools.length) return null;
      return (
        <div key="tools">
          <h3 className="sec">{sec.title} <span className="muted tiny">· {a.tools.length} · ≈{fmtTok(a.catTokens.tools || 0)} tok</span></h3>
          <div className="card">
            {a.tools.map((t, i) => (
              <div key={i} className="msg system">
                <div className="rail" />
                <div className="content">
                  <div className="head"><code>{t.name}</code><span className="muted tiny act">≈{fmtTok(t.tokens)} tok</span></div>
                  {t.desc ? <div className="muted tiny">{t.desc}</div> : null}
                </div>
              </div>
            ))}
          </div>
        </div>
      );
    }
    const segs = a.segments.filter((s) => s.cat === sec.cat);
    if (!segs.length) return null;
    const tok = segs.reduce((x, s) => x + s.tokens, 0);
    return (
      <div key={sec.cat}>
        <h3 className="sec">{sec.title} <span className="muted tiny">· {segs.length} · ≈{fmtTok(tok)} tok</span></h3>
        <div className="card">
          {segs.map((s, i) => <CwBlock key={i} label={s.label} cls={CW_BADGE[s.cat]} tokens={s.tokens} text={s.text} />)}
        </div>
      </div>
    );
  }).filter(Boolean);
  return <div>{sections}</div>;
}

function CwRawView({ a }: { a: CwAnalysis }) {
  return (
    <div className="card">
      {a.raw.map((m, i) => <CwBlock key={i} label={m.label} cls={m.cls} text={m.text} />)}
      {a.tools.length ? (
        <div className="msg system">
          <div className="rail" />
          <div className="content">
            <div className="head"><span className="badge system">Tools</span><span className="muted tiny act">{a.tools.length} available</span></div>
            <CwMd text={a.tools.map((t) => '- `' + t.name + '`' + (t.desc ? ' — ' + t.desc : '')).join('\n')} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function CwTokens({ a }: { a: CwAnalysis }) {
  const rows = CW_SECTIONS
    .map((s) => ({ title: s.title, cls: CW_BADGE[s.cat], tok: a.catTokens[s.cat] || 0 }))
    .filter((r) => r.tok > 0);
  const max = rows.reduce((m, r) => Math.max(m, r.tok), 1);
  return (
    <div>
      <div className="card" style={{ padding: 14 }}>
        <div style={{ fontSize: 26, fontWeight: 700, lineHeight: 1.1 }}>
          {fmtTok(a.total)}<span className="muted" style={{ fontSize: 13, fontWeight: 400 }}> tokens sent each call</span>
        </div>
        <div className="muted tiny" style={{ marginTop: 3 }}>estimated at ~4 chars/token, summed across everything in the forwarded payload</div>
      </div>
      <h3 className="sec">Breakdown by section</h3>
      <div className="card">
        {rows.map((r, i) => {
          const pct = a.total > 0 ? Math.round((r.tok / a.total) * 100) : 0;
          return (
            <div key={i} style={{ marginBottom: 10 }}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className={'badge ' + r.cls}>{r.title}</span>
                <span className="muted tiny">{fmtTok(r.tok)} tok · {pct}%</span>
              </div>
              <div style={{ height: 8, borderRadius: 5, overflow: 'hidden', marginTop: 5, background: 'rgba(127,127,127,0.18)' }}>
                <div style={{ width: Math.max(2, Math.round((r.tok / max) * 100)) + '%', height: '100%', background: 'var(--accent)' }} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Graph view: animated per-request composition timeline ──────────────────
type TlBlock = {
  id: string | null; fp: string; role: string; tokens: number; after_tokens?: number;
  preview: string; status: 'kept' | 'changed' | 'removed' | 'added'; technique: string;
};
type TlTurn = {
  index: number; ts: number; surface: string; model: string;
  before_tokens: number; after_tokens: number; new_fps: string[];
  events: Array<Record<string, any>>; blocks: TlBlock[];
};

// Role → block class + 1-letter glyph. Human blocks that look like injected
// context (system reminders, claudeMd, …) render brown like everywhere else.
function tlRole(b: TlBlock): { cls: string; glyph: string; label: string } {
  const r = String(b.role || '').toLowerCase();
  if (r === 'ai' || r === 'assistant') return { cls: 'assistant', glyph: 'A', label: 'Assistant' };
  if (r === 'tool') return { cls: 'tool', glyph: 'T', label: 'Tool' };
  if (r === 'human' || r === 'user') {
    return CONTEXT_RE.test(b.preview || '')
      ? { cls: 'context', glyph: 'C', label: 'Context' }
      : { cls: 'user', glyph: 'U', label: 'User' };
  }
  return { cls: 'system', glyph: 'S', label: 'System' };
}

const TL_TECH_LABEL: Record<string, string> = {
  tool_result_trimming: 'Tool trimming',
  summarization: 'Summarization',
  sliding_window: 'Sliding window',
  image_eviction: 'Image eviction',
  visual_method: 'Visual method',
  cache_breakpoints: 'Cache breakpoints',
  manual_removal: 'Manual removal',
};

function CwGraph({ conv }: { conv: string }) {
  const [turns, setTurns] = useState<TlTurn[] | null>(null);
  const [liveTurn, setLiveTurn] = useState(-1); // turn index to animate
  const [replayKey, setReplayKey] = useState(0);
  const [replaying, setReplaying] = useState(false);
  const [tip, setTip] = useState<{ x: number; y: number; b: TlBlock } | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const newestRef = useRef(-1);

  const load = useCallback(() => {
    rpc('contextTimeline', { conv, limit: 50 }).then((d: any) => {
      const list: TlTurn[] = d.turns || [];
      const newest = list.length ? list[list.length - 1].index : -1;
      // Animate only when a genuinely new turn arrived after the initial fetch.
      if (newestRef.current >= 0 && newest !== newestRef.current) setLiveTurn(newest);
      newestRef.current = newest;
      setTurns(list);
    }).catch(() => setTurns([]));
  }, [conv]);
  useEffect(() => { newestRef.current = -1; setLiveTurn(-1); load(); }, [load]);

  const onEvent = useCallback((e: AcmEvent) => {
    if (e.type === 'turn' && (!e.conv || e.conv === conv)) load();
  }, [conv, load]);
  useAcmEvents(onEvent);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [turns?.length]);

  const replay = useCallback(() => {
    setLiveTurn(-1);
    setReplaying(true);
    setReplayKey((k) => k + 1);
  }, []);
  useEffect(() => {
    if (!replaying) return;
    // Long enough for the last staggered row to finish entering.
    const n = turns?.length || 0;
    const id = setTimeout(() => setReplaying(false), n * 120 + 1500);
    return () => clearTimeout(id);
  }, [replaying, replayKey, turns?.length]);

  if (turns === null) return <Loading />;
  if (!turns.length) {
    return (
      <div className="empty">
        <p>No requests recorded yet.</p>
        <p className="tiny">The timeline records every request from now on — send a message in
          this chat and each call's context will appear here as a row of blocks.</p>
      </div>
    );
  }

  const truncated = turns[0].index > 1;
  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
        <span className="muted tiny">
          Each row is one request — blocks are the messages it carried, sized by tokens.
          {truncated ? ` Showing the last ${turns.length} requests.` : ''}
        </span>
        <button className="btn sec sm" onClick={replay} title="Replay the timeline from the first recorded request">▶ Replay</button>
      </div>
      <div className={'cwg-rows' + (replaying ? ' replaying' : '')} key={replayKey}>
        {turns.map((t, ti) => {
          const techEvents = t.events.filter((e) => e.type !== 'notice' && e.type !== 'cache_breakpoints');
          const live = t.index === liveTurn;
          const newFps = new Set(t.new_fps || []);
          // Stagger only the tail of a huge first row: cap entrance delays.
          let enterSeq = 0;
          return (
            <div key={t.index} style={replaying ? { animationDelay: (ti * 120) + 'ms' } : undefined}
              className={'cwg-turn' + (live || replaying ? ' live' : '')}>
              {techEvents.length > 0 && (
                <div className="cwg-between">
                  {techEvents.map((e, i) => (
                    <span key={i} className={'cwg-chip' + (e.type === 'summarization' ? ' summ' : '')}>
                      ⚙ {TL_TECH_LABEL[String(e.type)] || String(e.type)}
                      {Number(e.freed_tokens) > 0 ? ` −${fmtTok(Number(e.freed_tokens))} tok` : ''}
                    </span>
                  ))}
                </div>
              )}
              <div className="cwg-row">
                <div className="cwg-gutter">
                  <div>#{t.index} · {rel(t.ts)}</div>
                  <div>≈{fmtTok(t.after_tokens)} tok</div>
                </div>
                <div className="cwg-strip">
                  {t.blocks.map((b, bi) => {
                    const { cls, glyph, label } = tlRole(b);
                    const removed = b.status === 'removed';
                    const entering = (live || replaying) && !removed
                      && (b.status === 'added' || newFps.has(b.fp));
                    const delay = entering ? Math.min(enterSeq++, 12) * 60 : 0;
                    const w = removed ? 0.0001 : Math.max(1, b.after_tokens ?? b.tokens);
                    return (
                      <div
                        key={bi}
                        className={
                          'cwg-block ' + cls
                          + (removed ? ' removed' : '')
                          + (entering ? ' entering' : '')
                          + (b.status === 'changed' && (live || replaying) ? ' changed live' : '')
                        }
                        style={{ flexGrow: w, animationDelay: delay ? delay + 'ms' : undefined }}
                        onMouseEnter={(ev) => setTip({ x: ev.clientX, y: ev.clientY, b })}
                        onMouseMove={(ev) => setTip({ x: ev.clientX, y: ev.clientY, b })}
                        onMouseLeave={() => setTip(null)}
                      >
                        {!removed && <span className="cwg-glyph" aria-label={label}>{glyph}</span>}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      {tip && (
        <div className="cwg-tip" style={{ left: Math.min(tip.x + 12, window.innerWidth - 330), top: tip.y + 14 }}>
          <div className="row" style={{ gap: 6, marginBottom: 4 }}>
            <span className={'badge ' + tlRole(tip.b).cls}>{tlRole(tip.b).label}</span>
            <span className="muted tiny">
              {tip.b.status === 'changed'
                ? `≈${fmtTok(tip.b.tokens)} → ${fmtTok(tip.b.after_tokens || 0)} tok`
                : `≈${fmtTok(tip.b.tokens)} tok`}
              {tip.b.status !== 'kept' ? ` · ${tip.b.status}` : ''}
              {tip.b.technique ? ` · ${TL_TECH_LABEL[tip.b.technique] || tip.b.technique}` : ''}
            </span>
          </div>
          <div className="tiny" style={{ opacity: 0.85 }}>{tip.b.preview || '(empty)'}</div>
        </div>
      )}
    </div>
  );
}

export function ContextWindow({ standalone, conv }: { standalone?: boolean; conv?: string }) {
  const [theme] = useState<Theme>(() => (getState().theme as Theme) || 'auto');
  const [convs, setConvs] = useState<any[]>([]);
  const [sel, setSel] = useState(conv || '');
  const [data, setData] = useState<any>(null);
  const [view, setView] = useState<'proper' | 'raw' | 'tokens' | 'graph'>('proper');
  const [ready, setReady] = useState(!!conv);

  const loadConvs = useCallback(() => {
    rpc('conversations').then((d: any) => {
      const list = d.conversations || [];
      setConvs(list);
      setSel((cur) => cur || (list[0] ? list[0].key : ''));
    }).catch(() => {}).finally(() => setReady(true));
  }, []);
  useEffect(() => {
    // Pinned to one chat (chat detail) — no conversation picker needed.
    if (conv) { setSel(conv); setReady(true); return; }
    loadConvs();
  }, [conv, loadConvs]);

  const load = useCallback(() => {
    rpc('contextWindow', { conv: sel }).then((d: any) => setData(d)).catch(() => setData(null));
  }, [sel]);
  useEffect(() => {
    load();
    // Slow fallback poll; realtime events below do the heavy lifting.
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  // When following, the panel tracks whichever chat last sent a turn — so it
  // always shows the context window for the chat you're actively using in the
  // IDE. Turned off the moment you pick a chat by hand. Pinned panels (a `conv`
  // prop, i.e. chat detail) never follow.
  const [follow, setFollow] = useState(!conv);

  // Realtime: refresh the moment this chat's window changes. Events for other
  // chats only refresh the picker (titles/token counts), not the open data.
  const onEvent = useCallback((e: AcmEvent) => {
    if (!conv) loadConvs();
    if (!conv && follow && e.type === 'turn' && e.conv) { setSel(e.conv); return; }
    if (!e.conv || e.conv === sel) load();
  }, [conv, sel, follow, load, loadConvs]);
  useAcmEvents(onEvent);

  const pick = useCallback((key: string) => { setFollow(false); setSel(key); }, []);

  const a = data ? cwAnalyze(data) : null;
  const has = !!(a && (a.segments.length || a.tools.length));
  const views: [typeof view, string][] = [['proper', 'Proper format'], ['raw', 'Raw'], ['tokens', 'Token usage'], ['graph', 'Graph']];

  const controls = (
    <>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row" style={{ gap: 6, alignItems: 'center' }}>
          {!conv && (convs.length > 0 ? (
            <select value={sel} onChange={(e) => pick(e.target.value)} style={{ maxWidth: 280 }}>
              {convs.map((c) => <option key={c.key} value={c.key}>{(c.title || 'Untitled chat') + ' · ' + shortId(c.key)}</option>)}
            </select>
          ) : <span className="muted tiny">no conversations yet</span>)}
          {!conv && (follow
            ? <span className="badge" title="Tracking whichever chat you're actively using">● following active chat</span>
            : <button className="btn sec sm" onClick={() => setFollow(true)} title="Track the chat you're actively using">Follow active</button>
          )}
        </div>
        <div className="row" style={{ gap: 4 }}>
          {views.map(([k, label]) => (
            <button key={k} className={'btn sm ' + (view === k ? '' : 'ghost')} onClick={() => setView(k)}>{label}</button>
          ))}
        </div>
      </div>
      {a && data && data.ts ? (
        <p className="muted tiny" style={{ marginTop: 8 }}>
          {data.model ? <>Model <code>{data.model}</code> · </> : null}
          {data.surface || '—'} · <strong>{fmtTok(a.total)}</strong> tokens sent each call · {a.segments.length} parts · captured {rel(data.ts)}
        </p>
      ) : null}
      <p className="muted tiny" style={{ marginTop: 4 }}>
        The exact message array the gateway forwards to the model every call, after
        context-management (drops, trimming, summarisation) is applied.
      </p>
    </>
  );

  const inner = !ready ? <Loading /> : view === 'graph' ? <CwGraph conv={sel} /> : !has ? (
    <div className="empty">
      <p>No model call captured yet.</p>
      <p className="tiny">Point your IDE's model endpoint at the gateway and send a message —
        the exact payload we forward will appear here.</p>
    </div>
  ) : view === 'proper' ? <CwProper a={a!} />
    : view === 'raw' ? <CwRawView a={a!} />
    : <CwTokens a={a!} />;

  if (standalone) {
    return (
      <div className="acm" data-theme={theme}>
        <header className="hd">
          <svg className="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
            <path d="m3 12 9 4.5L21 12M3 16.5l9 4.5 9-4.5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
          <div style={{ minWidth: 0 }}>
            <div className="title">ACM Context Window</div>
            <div className="sub" title={sel}>
              {sel ? (convs.find((c) => c.key === sel)?.title || 'Untitled chat') + ' · ' + shortId(sel) : 'what we send to the model each call'}
            </div>
          </div>
        </header>
        <main className="body">{controls}{inner}</main>
      </div>
    );
  }
  return <div>{controls}{inner}</div>;
}
