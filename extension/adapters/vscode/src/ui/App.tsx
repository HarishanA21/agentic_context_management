import { useEffect, useState, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import { rpc, getState, setState, openChat, projectRoot, chatConv } from './bridge';

type Theme = 'auto' | 'light' | 'dark';
const THEME_ICON: Record<Theme, string> = { auto: '◐', light: '☀', dark: '☾' };
const NEXT_THEME: Record<Theme, Theme> = { auto: 'light', light: 'dark', dark: 'auto' };

const clone = <T,>(o: T): T => JSON.parse(JSON.stringify(o));
const useReload = (): [number, () => void] => {
  const [n, setN] = useState(0);
  return [n, useCallback(() => setN((x) => x + 1), [])];
};
function rel(ts: number): string {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
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

const TABS = ['Overview', 'Chats', 'Techniques', 'Profiles', 'Providers', 'Memory'];

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
        {tab === 'Chats' && <Chats />}
        {tab === 'Techniques' && <Techniques />}
        {tab === 'Profiles' && <Profiles />}
        {tab === 'Providers' && <Providers />}
        {tab === 'Memory' && <Memory />}
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
  const orig = live + saved;
  const livePct = orig > 0 ? Math.max(2, Math.round((live / orig) * 100)) : 100;
  const savedPct = orig > 0 ? Math.round((saved / orig) * 100) : 0;

  return (
    <div>
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

      {/* Connection / routing details */}
      <h3 className="sec">Gateway</h3>
      <div className="card">
        <div className="kv">
          <span className="k">Upstream</span>
          <span><span className="dot ok" /> <code title={status.upstream}>{hostOf(status.upstream)}</code></span>
          <span className="k">Default provider</span>
          <span>{status.providers?.default || <span className="muted">env fallback</span>}{(status.providers?.configured || []).length ? <span className="muted tiny"> · {(status.providers.configured).length} configured</span> : null}</span>
          <span className="k">Tool surface</span><span>{prettySurface(status.tool_surface)}</span>
          <span className="k">Config</span>
          <span className="tiny"><code title={status.config_path}>{String(status.config_path || '').split('/').pop()}</code></span>
        </div>
      </div>

      <h3 className="sec">Active techniques <span className="muted tiny">({activeCount} on)</span></h3>
      <div className="chips">
        {Object.entries(tech).map(([k, v]) => (
          <span key={k} className={'chip ' + (on(v) ? 'on' : 'off')}>
            <span className={'dot ' + (on(v) ? 'ok' : '')} />{k}{typeof v === 'string' && v !== 'off' ? ': ' + v : ''}
          </span>
        ))}
      </div>

      <h3 className="sec">Recent activity</h3>
      {events.length === 0 ? (
        <p className="muted tiny">No edits yet. Route an IDE chat through the gateway to see techniques fire.</p>
      ) : (
        <ul className="timeline">
          {events.map((e: any, i: number) => (
            <li key={i}>
              <span className="t">{e.type}</span>
              <span className="muted tiny">
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

function Chats() {
  const [wins, setWins] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('contextWindows', { project: projectRoot }).then((d: any) => {
      setWins(d.windows || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [n]);

  const del = (e: any, id: string) => {
    e.stopPropagation();
    if (!window.confirm(
      'Delete this context window and all its ACM state (drop-list, summaries)? ' +
      'The chat in your IDE is unaffected.')) return;
    setBusy(true);
    rpc('deleteWindow', { conv: id }).then(reload).finally(() => setBusy(false));
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
        <button className="btn sec sm" onClick={reload}>Refresh</button>
      </div>
      <p className="muted tiny">Each chat is its own context window with its own techniques. Open one to
        see exactly what's sent to the model and tune that chat's settings.</p>
      <div className="conv-list">
        {wins.map((c) => (
          <div key={c.id} className="conv" onClick={() => openChat(c.id)} title="Open chat detail">
            <span className="id" style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.title || 'Untitled chat'}
              </span>
              <span className="muted tiny" style={{ fontFamily: 'var(--vscode-editor-font-family, monospace)' }}>{c.id}</span>
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
        ))}
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
          <div className="title">{win?.title || 'Chat'}</div>
          <div className="sub" style={{ fontFamily: 'var(--vscode-editor-font-family, monospace)' }}>{conv}</div>
        </div>
      </header>
      <main className="body">
        <div className="two-col">
          <section className="col">
            <h3 className="sec">Context window <span className="muted tiny">— what's sent to the model</span></h3>
            <ContextWindow conv={conv} />
          </section>
          <section className="col">
            <h3 className="sec">Settings <span className="muted tiny">— this chat only</span></h3>
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

// One transcript row: role badge, preview, remove/restore, and — for tool
// results — a "View image" toggle that shows the visual-method page render.
function MsgRow({ m, conv, onAct }: { m: any; conv: string; onAct: (method: string, fp: string) => void }) {
  const r = classify(m);
  const [imgs, setImgs] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [full, setFull] = useState<string | null>(null); // expanded full text
  const [fullBusy, setFullBusy] = useState(false);
  const isTool = String(m.role || '').toLowerCase() === 'tool';
  const truncated = String(m.preview || '').endsWith('…');

  const viewImages = async () => {
    if (imgs) { setImgs(null); return; } // toggle closed
    setBusy(true); setErr('');
    try {
      const d: any = await rpc('messageImages', { fp: m.fp, conv });
      if (d?.error) setErr(d.error);
      const list = d.images || [];
      setImgs(list);
      if (list.length === 0 && !d?.error) setErr('output too small to rasterise');
    } catch (e: any) {
      setErr(e.message || 'failed');
    } finally {
      setBusy(false);
    }
  };

  const toggleFull = async () => {
    if (full !== null) { setFull(null); return; } // collapse
    setFullBusy(true);
    try {
      const d: any = await rpc('messageText', { fp: m.fp, conv });
      setFull(d?.text || m.preview || '');
    } catch {
      setFull(m.preview || '');
    } finally {
      setFullBusy(false);
    }
  };

  return (
    <div className={'msg ' + r.cls + (m.dropped ? ' dropped' : '')}>
      <div className="rail" />
      <div className="content">
        <div className="head">
          <span className={'badge ' + r.cls}>{r.label}</span>
          <span className="muted tiny"><code>{m.fp}</code></span>
          {m.dropped && <span className="muted tiny" style={{ color: 'var(--bad, #e06c6c)' }}>removed</span>}
          <span className="act">
            {isTool && (
              <button className="btn ghost sm" onClick={viewImages}>
                {busy ? '…' : imgs ? 'Hide' : '🖼 View'}
              </button>
            )}
            {m.dropped
              ? <button className="btn ghost sm" onClick={() => onAct('restoreMessage', m.fp)}>Restore</button>
              : <button className="btn ghost sm" onClick={() => { if (confirmDrop()) onAct('dropMessage', m.fp); }}>Remove</button>}
          </span>
        </div>
        {/* click to expand the full message (rows only store a short preview) */}
        <div
          className="text"
          style={{ cursor: 'pointer', whiteSpace: full !== null ? 'pre-wrap' : 'normal' }}
          title={full !== null ? 'Click to collapse' : 'Click to expand full message'}
          onClick={toggleFull}
        >
          {fullBusy
            ? <span className="muted">loading…</span>
            : full !== null
              ? full
              : (m.preview || <span className="muted">(empty)</span>)}
        </div>
        {truncated && (
          <button className="btn ghost sm" style={{ marginTop: 4 }} onClick={toggleFull}>
            {fullBusy ? '…' : full !== null ? 'Show less' : 'Show full message'}
          </button>
        )}
        {err && <div className="muted tiny" style={{ marginTop: 4 }}>{err}</div>}
        {imgs && imgs.length > 0 && (
          <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
            {imgs.map((src, i) => (
              <div key={i} style={{ border: '1px solid var(--vscode-panel-border)', borderRadius: 4, overflow: 'hidden', background: '#fff' }}>
                <div className="muted tiny" style={{ padding: '2px 6px' }}>page {i + 1}/{imgs.length}</div>
                <img src={src} alt={'page ' + (i + 1)} style={{ width: '100%', display: 'block' }} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
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

// A collapsible turn: user prompt + token total at the top; expand to inspect
// and remove the assistant/tool messages it produced.
function TurnGroup({ turn, conv, onAct }: { turn: ChatTurn; conv: string; onAct: (m: string, fp: string) => void }) {
  const [open, setOpen] = useState(false);
  const total =
    (turn.user ? (turn.user.tokens || 0) : 0) +
    turn.children.reduce((a, m) => a + (m.tokens || 0), 0);
  const isContextGroup = !turn.user;
  const badgeCls = isContextGroup ? 'context' : 'user';
  const badgeLabel = isContextGroup ? 'Context' : 'User';
  const preview = isContextGroup
    ? `${turn.children.length} context / system message${turn.children.length === 1 ? '' : 's'}`
    : turn.user.preview || '(empty)';
  return (
    <div className="turn" style={{ borderBottom: '1px solid var(--vscode-panel-border)', paddingBottom: 6, marginBottom: 6 }}>
      <div className="row" onClick={() => setOpen((v) => !v)} style={{ cursor: 'pointer', gap: 6 }}>
        <span className="muted">{open ? '▾' : '▸'}</span>
        <span className={'badge ' + badgeCls}>{badgeLabel}</span>
        <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, fontSize: 13 }}>{preview}</span>
        <span className="muted tiny" style={{ whiteSpace: 'nowrap' }}>{turn.children.length} msg</span>
        <span className="muted tiny" style={{ whiteSpace: 'nowrap' }}>≈{fmtTok(total)} tok</span>
      </div>
      {open && (
        <div style={{ marginTop: 6 }}>
          {turn.user && <MsgRow m={turn.user} conv={conv} onAct={onAct} />}
          {turn.children.map((m) => <MsgRow key={m.fp} m={m} conv={conv} onAct={onAct} />)}
        </div>
      )}
    </div>
  );
}

// ── Cleanup (task-aware relevance suggestions) ─────────────────────────
function Cleanup({ conv: fixedConv }: { conv?: string } = {}) {
  const [convs, setConvs] = useState<any[]>([]);
  const [sel, setSel] = useState<string>(fixedConv || '');
  const [sugs, setSugs] = useState<any[] | null>(null);
  const [info, setInfo] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  // Per-chat (fixedConv) also shows the message-level drop transcript.
  const [msgs, setMsgs] = useState<any[]>([]);
  const [raw, setRaw] = useState(true);
  const [mn, mreload] = useReload();

  useEffect(() => {
    if (fixedConv) { setSel(fixedConv); return; }
    rpc('conversations').then((d: any) => {
      const list = d.conversations || [];
      setConvs(list);
      setSel((cur) => cur || (list[0] ? list[0].key : ''));
    }).catch(() => {});
  }, [fixedConv]);

  useEffect(() => {
    if (!fixedConv || !sel) { setMsgs([]); return; }
    rpc('messages', { conv: sel }).then((d: any) => setMsgs(d.messages || []));
  }, [fixedConv, sel, mn]);

  const act = (method: string, fp: string) => rpc(method, { fp, conv: sel }).then(mreload);

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

      {fixedConv && (
        <>
          <div className="row" style={{ alignItems: 'center', justifyContent: 'space-between', marginTop: 14 }}>
            <h3 className="sec" style={{ margin: 0 }}>
              Messages <span className="muted tiny">— {msgs.length}, in context order</span>
            </h3>
            <div className="row" style={{ gap: 4 }}>
              <button className={'btn sm ' + (raw ? '' : 'ghost')} onClick={() => setRaw(true)}>Raw</button>
              <button className={'btn sm ' + (raw ? 'ghost' : '')} onClick={() => setRaw(false)}>Grouped</button>
            </div>
          </div>
          <p className="muted tiny" style={{ marginTop: -4 }}>
            Removing hides a message from the model on every future turn (the IDE still shows it).
          </p>
          <div className="card">
            {msgs.length === 0 ? <p className="muted tiny">No messages recorded for this chat yet.</p> :
              raw
                ? msgs.map((m, i) => <MsgRow key={m.fp ?? i} m={m} conv={sel} onAct={act} />)
                : groupTurns(msgs).map((t, i) => (
                    <TurnGroup key={t.user?.fp ?? 'turn-' + i} turn={t} conv={sel} onAct={act} />
                  ))}
          </div>
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
  const [msg, setMsg] = useState('');
  const [n, reload] = useReload();
  useEffect(() => {
    if (perChat) {
      rpc('getContextWindow', { conv }).then((w: any) => { setProf(w.profile); setVm(null); });
    } else {
      rpc('getProfile').then((p: any) => {
        setProf(p.active);
        setVm(p.visual_method || { enabled: false, trigger_tokens: 500, only_tools: [], exclude_tools: [] });
      });
    }
  }, [n, conv]);
  if (!prof) return <Loading />;
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

// ── Profiles ───────────────────────────────────────────────────────────
function Profiles() {
  const [p, setP] = useState<any>(null);
  const [msg, setMsg] = useState('');
  const [n, reload] = useReload();
  useEffect(() => { rpc('getProfile').then(setP); }, [n]);
  if (!p) return <Loading />;
  const apply = async (name: string) => {
    setMsg('applying ' + name + '…');
    try { await rpc('setPreset', { name }); setMsg(name + ' applied ✓'); reload(); }
    catch (e: any) { setMsg('Error: ' + e.message); }
  };
  return (
    <div>
      <p className="muted tiny">Presets bundle techniques for a use case. Applying one overwrites your technique settings (your <code>visual_method</code> choice is kept).</p>
      {(p.presets || []).map((preset: any) => (
        <div className="card" key={preset.name}>
          <div className="row">
            <strong>{preset.name}</strong>
            <button className="btn sm" style={{ marginLeft: 'auto' }} onClick={() => apply(preset.name)}>Apply</button>
          </div>
          <div className="desc muted tiny" style={{ marginTop: 4 }}>{preset.summary}</div>
        </div>
      ))}
      <p className="toast">{msg}</p>
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

export function ContextWindow({ standalone, conv }: { standalone?: boolean; conv?: string }) {
  const [theme] = useState<Theme>(() => (getState().theme as Theme) || 'auto');
  const [convs, setConvs] = useState<any[]>([]);
  const [sel, setSel] = useState(conv || '');
  const [data, setData] = useState<any>(null);
  const [view, setView] = useState<'proper' | 'raw' | 'tokens'>('proper');
  const [ready, setReady] = useState(!!conv);

  useEffect(() => {
    // Pinned to one chat (chat detail) — no conversation picker needed.
    if (conv) { setSel(conv); setReady(true); return; }
    rpc('conversations').then((d: any) => {
      const list = d.conversations || [];
      setConvs(list);
      setSel((cur) => cur || (list[0] ? list[0].key : ''));
    }).catch(() => {}).finally(() => setReady(true));
  }, [conv]);

  const load = useCallback(() => {
    rpc('contextWindow', { conv: sel }).then((d: any) => setData(d)).catch(() => setData(null));
  }, [sel]);
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  const a = data ? cwAnalyze(data) : null;
  const has = !!(a && (a.segments.length || a.tools.length));
  const views: [typeof view, string][] = [['proper', 'Proper format'], ['raw', 'Raw'], ['tokens', 'Token usage']];

  const controls = (
    <>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row" style={{ gap: 6 }}>
          {!conv && (convs.length > 0 ? (
            <select value={sel} onChange={(e) => setSel(e.target.value)} style={{ maxWidth: 280 }}>
              {convs.map((c) => <option key={c.key} value={c.key}>{c.title || c.key}</option>)}
            </select>
          ) : <span className="muted tiny">no conversations yet</span>)}
          <button className="btn sec sm" onClick={load}>Refresh</button>
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

  const inner = !ready ? <Loading /> : !has ? (
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
            <div className="sub">what we send to the model each call</div>
          </div>
        </header>
        <main className="body">{controls}{inner}</main>
      </div>
    );
  }
  return <div>{controls}{inner}</div>;
}
