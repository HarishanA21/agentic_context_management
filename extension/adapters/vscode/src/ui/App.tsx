import { useEffect, useState, useCallback } from 'react';
import { rpc } from './bridge';

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
const ROLE = (r: string): { cls: string; label: string } => {
  if (r === 'human') return { cls: 'user', label: 'User' };
  if (r === 'ai') return { cls: 'assistant', label: 'Assistant' };
  if (r === 'tool') return { cls: 'tool', label: 'Tool' };
  return { cls: 'system', label: 'System' };
};

const TABS = ['Overview', 'Chats', 'Techniques', 'Profiles', 'Providers', 'Memory'];

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

  const poll = useCallback(() => {
    rpc('status').then((s) => { setStatus(s); setReachable(true); }).catch(() => setReachable(false));
  }, []);
  useEffect(() => {
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, [poll]);

  return (
    <div className="acm">
      <header className="hd">
        <svg className="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          <path d="m3 12 9 4.5L21 12M3 16.5l9 4.5 9-4.5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
        <div>
          <div className="title">ACM Context Management</div>
          <div className="sub">{status?.upstream || 'local gateway'}</div>
        </div>
        <span className="pill">
          <span className={'dot ' + (reachable === null ? '' : reachable ? 'ok' : 'bad')} />
          {reachable === null ? 'Connecting…' : reachable ? 'Connected' : 'Offline'}
        </span>
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
function Overview({ status, reachable, onRefresh }: any) {
  if (reachable === false) return <p className="muted">Gateway offline — start it to see status.</p>;
  if (!status) return <Loading />;
  const tech = status.techniques || {};
  const on = (v: any) => v && v !== 'off';
  const events = (status.last_events || []).slice(-12).reverse();
  return (
    <div>
      <div className="card">
        <div className="kv">
          <span className="k">Upstream</span><span><code>{status.upstream}</code></span>
          <span className="k">Tool surface</span><span><code>{status.tool_surface}</code></span>
          <span className="k">Default provider</span><span>{status.providers?.default || 'env fallback'}</span>
          <span className="k">Config</span><span className="tiny"><code>{status.config_path}</code></span>
        </div>
      </div>

      <h3 className="sec">Active techniques</h3>
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

// ── Chats (agent conversations) ────────────────────────────────────────
function Chats() {
  const [convs, setConvs] = useState<any[]>([]);
  const [sel, setSel] = useState<string>('');
  const [msgs, setMsgs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [n, reload] = useReload();

  useEffect(() => {
    rpc('conversations').then((d: any) => {
      const list = d.conversations || [];
      setConvs(list);
      setSel((cur) => cur || (list[0] ? list[0].key : ''));
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [n]);

  useEffect(() => {
    if (!sel) { setMsgs([]); return; }
    rpc('messages', { conv: sel }).then((d: any) => setMsgs(d.messages || []));
  }, [sel, n]);

  const act = (method: string, fp: string) => rpc(method, { fp, conv: sel }).then(reload);

  if (loading) return <Loading />;
  if (convs.length === 0)
    return (
      <div className="empty">
        <p>No agent conversations yet.</p>
        <p className="tiny">Point your IDE's model endpoint at the gateway and chat, then come back.
          Conversations the gateway sees appear here, where you can inspect and remove messages.</p>
        <button className="btn sec sm" onClick={reload}>Refresh</button>
      </div>
    );

  return (
    <div>
      <h3 className="sec">Conversations</h3>
      <div className="conv-list">
        {convs.map((c) => (
          <div key={c.key} className={'conv' + (c.key === sel ? ' active' : '')} onClick={() => setSel(c.key)}>
            <span className="id">{c.key}</span>
            <span className="meta">
              <span className="count-badge">{c.count} msg</span>
              {c.dropped ? <> · <span className="muted">{c.dropped} removed</span></> : null}
              <div>{rel(c.ts)}</div>
            </span>
          </div>
        ))}
      </div>

      <h3 className="sec">Transcript <span className="muted tiny">— removing hides a message from the model on every future turn (the IDE still shows it)</span></h3>
      <div className="card">
        {msgs.length === 0 ? <p className="muted tiny">No messages recorded for this conversation.</p> :
          msgs.map((m) => {
            const r = ROLE(m.role);
            return (
              <div key={m.fp} className={'msg ' + r.cls + (m.dropped ? ' dropped' : '')}>
                <div className="rail" />
                <div className="content">
                  <div className="head">
                    <span className={'badge ' + r.cls}>{r.label}</span>
                    <span className="muted tiny"><code>{m.fp}</code></span>
                    <span className="act">
                      {m.dropped
                        ? <button className="btn ghost sm" onClick={() => act('restoreMessage', m.fp)}>Restore</button>
                        : <button className="btn ghost sm" onClick={() => act('dropMessage', m.fp)}>Remove</button>}
                    </span>
                  </div>
                  <div className="text">{m.preview || <span className="muted">(empty)</span>}</div>
                </div>
              </div>
            );
          })}
      </div>
      <p><button className="btn sec sm" onClick={reload}>Refresh</button></p>
    </div>
  );
}

// ── Techniques ─────────────────────────────────────────────────────────
function Techniques() {
  const [prof, setProf] = useState<any>(null);
  const [vm, setVm] = useState<any>(null);
  const [msg, setMsg] = useState('');
  const [n, reload] = useReload();
  useEffect(() => {
    rpc('getProfile').then((p: any) => {
      setProf(p.active);
      setVm(p.visual_method || { enabled: false, trigger_tokens: 500, only_tools: [], exclude_tools: [] });
    });
  }, [n]);
  if (!prof || !vm) return <Loading />;
  const cm = prof.context_management;
  const setCM = (key: string, field: string, value: any) => { const x = clone(prof); x.context_management[key][field] = value; setProf(x); };
  const save = async () => {
    setMsg('saving…');
    try { await rpc('setProfileBody', { body: prof, visual_method: vm }); setMsg('Saved ✓'); }
    catch (e: any) { setMsg('Error: ' + e.message); }
  };
  return (
    <div>
      <p className="muted tiny">Toggle techniques the gateway applies to every turn. Changes save to your config and take effect on the next request.</p>
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
