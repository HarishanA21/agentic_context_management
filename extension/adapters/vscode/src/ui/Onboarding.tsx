// First-run experience. Shows the user the path their context takes —
//   This PC  →  ACM Gateway  →  Model server
// — checks each hop live, and once everything is healthy hands them a copy-paste
// recipe to try it from their agent (Claude Code terminal for now). A "Get
// started" button dismisses it into the main panel.

import { useCallback, useEffect, useState } from 'react';
import { rpc, gatewayUrl } from './bridge';

type HopState = 'checking' | 'ok' | 'bad';

interface Health {
  pc: HopState; // the extension/webview is obviously up if this renders
  gateway: HopState; // can we reach the local acm-gateway?
  server: HopState; // does the gateway have a usable upstream model provider?
  upstream?: string; // host of the configured upstream, for display
  provider?: string; // default provider slug
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [h, setH] = useState<Health>({ pc: 'ok', gateway: 'checking', server: 'checking' });
  const [checking, setChecking] = useState(false);

  const probe = useCallback(async () => {
    setChecking(true);
    setH((p) => ({ ...p, gateway: 'checking', server: 'checking' }));
    try {
      const s: any = await rpc('status');
      const upstream = s?.upstream as string | undefined;
      const provider = s?.providers?.default as string | undefined;
      // Server hop is "ok" once the gateway reports an upstream it can forward to.
      const serverOk = Boolean(upstream);
      setH({
        pc: 'ok',
        gateway: 'ok',
        server: serverOk ? 'ok' : 'bad',
        upstream,
        provider,
      });
    } catch {
      setH({ pc: 'ok', gateway: 'bad', server: 'bad' });
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    probe();
  }, [probe]);

  const allOk = h.pc === 'ok' && h.gateway === 'ok' && h.server === 'ok';
  const gwHost = hostOf(gatewayUrl);

  return (
    <div className="onb">
      <div className="onb-hero">
        <div className="onb-logo">
          <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" width="34" height="34">
            <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
            <path d="m3 12 9 4.5L21 12M3 16.5l9 4.5 9-4.5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
        </div>
        <h1 className="onb-title">Welcome to ACM</h1>
        <p className="onb-tag">
          Agentic Context Management keeps your AI assistant's context window lean. Here's how your
          context flows — let's make sure every hop is live.
        </p>
      </div>

      {/* The picture: This PC → Gateway → Server, each a live-checked node. */}
      <div className="onb-flow">
        <Node
          icon={<IconPc />}
          label="This PC"
          sub="VSCode + ACM"
          state={h.pc}
        />
        <Link state={h.gateway} caption={gwHost} />
        <Node
          icon={<IconGateway />}
          label="ACM Gateway"
          sub="context engine"
          state={h.gateway}
        />
        <Link state={h.server} caption={h.upstream ? hostOf(h.upstream) : 'model API'} />
        <Node
          icon={<IconServer />}
          label="Model server"
          sub={h.provider || 'upstream'}
          state={h.server}
        />
      </div>

      {/* Per-hop status read-out. */}
      <div className="onb-checks">
        <Check state={h.pc} ok="Extension running" bad="Extension error" checkingText="Checking…" />
        <Check
          state={h.gateway}
          ok={`Gateway reachable at ${gwHost}`}
          bad="Gateway not reachable"
          checkingText="Connecting to gateway…"
        />
        <Check
          state={h.server}
          ok={h.upstream ? `Model server ready (${hostOf(h.upstream)})` : 'Model server ready'}
          bad="No model provider configured"
          checkingText="Checking model server…"
        />
      </div>

      {allOk ? (
        <ReadyToTest onDone={onDone} />
      ) : (
        <NotReady checking={checking} gateway={h.gateway} server={h.server} onRetry={probe} onSkip={onDone} />
      )}
    </div>
  );
}

// ── all-green state: hand the user a recipe ───────────────────────────────────

function ReadyToTest({ onDone }: { onDone: () => void }) {
  return (
    <>
      <div className="onb-ready">
        <span className="onb-ready-dot" /> All systems active — you're ready to go.
      </div>

      <div className="onb-card">
        <div className="onb-card-head">
          <span className="onb-step-badge">Try it</span>
          <strong>Claude Code (terminal)</strong>
        </div>
        <p className="muted tiny" style={{ marginTop: 0 }}>
          ACM is already routing Claude Code through the gateway. Open a terminal in your project and
          run a normal Claude Code session — every turn now flows through ACM, and you can watch the
          context window live in the <strong>Context Window</strong> view.
        </p>

        <ol className="onb-recipe">
          <li>
            Open Claude Code in your project:
            <Code>claude</Code>
          </li>
          <li>
            Ask it anything, e.g.:
            <Code>Summarise what this project does and remember the key files.</Code>
          </li>
          <li>
            Have it use ACM memory across turns:
            <Code>#acmRecall what did we decide earlier?</Code>
          </li>
          <li>
            Watch the live context window: run <strong>ACM: Show Context Window</strong> from the
            command palette while the session runs.
          </li>
        </ol>

        <p className="muted tiny onb-note">
          More agents (Copilot, Cursor, Cline) — sample recipes coming soon. For now Claude Code is
          fully wired.
        </p>
      </div>

      <div className="onb-actions">
        <button className="btn onb-cta" onClick={onDone}>
          Get started →
        </button>
      </div>
    </>
  );
}

// ── not-ready state: tell them exactly what to fix ────────────────────────────

function NotReady({
  checking,
  gateway,
  server,
  onRetry,
  onSkip,
}: {
  checking: boolean;
  gateway: HopState;
  server: HopState;
  onRetry: () => void;
  onSkip: () => void;
}) {
  return (
    <>
      <div className="onb-card onb-card-warn">
        <strong>Almost there</strong>
        {gateway === 'bad' && (
          <p className="muted tiny">
            The gateway isn't responding yet. It usually starts itself on first launch — give it a
            few seconds, then retry. If it persists, run <Code inline>acm-gateway</Code> in a
            terminal, or check the <strong>ACM Gateway</strong> output channel.
          </p>
        )}
        {gateway === 'ok' && server === 'bad' && (
          <p className="muted tiny">
            The gateway is up, but no model provider is configured. Add one in the{' '}
            <strong>Providers</strong> tab, or set <Code inline>ACM_UPSTREAM_API_KEY</Code>. On a
            Claude subscription, routing works without a key — just retry.
          </p>
        )}
      </div>

      <div className="onb-actions">
        <button className="btn" onClick={onRetry} disabled={checking}>
          {checking ? 'Checking…' : 'Retry checks'}
        </button>
        <button className="btn sec" onClick={onSkip}>
          Skip for now
        </button>
      </div>
    </>
  );
}

// ── small presentational pieces ───────────────────────────────────────────────

function Node({
  icon,
  label,
  sub,
  state,
}: {
  icon: any;
  label: string;
  sub: string;
  state: HopState;
}) {
  return (
    <div className={'onb-node ' + state}>
      <div className="onb-node-icon">{icon}</div>
      <div className="onb-node-label">{label}</div>
      <div className="onb-node-sub muted tiny">{sub}</div>
      <div className="onb-node-state">
        {state === 'checking' ? (
          <span className="spin" />
        ) : state === 'ok' ? (
          <span className="onb-tick">✓</span>
        ) : (
          <span className="onb-cross">!</span>
        )}
      </div>
    </div>
  );
}

function Link({ state, caption }: { state: HopState; caption: string }) {
  return (
    <div className={'onb-link ' + state}>
      <div className="onb-link-line">
        <span className="onb-link-pulse" />
      </div>
      <div className="onb-link-cap muted tiny">{caption}</div>
    </div>
  );
}

function Check({
  state,
  ok,
  bad,
  checkingText,
}: {
  state: HopState;
  ok: string;
  bad: string;
  checkingText: string;
}) {
  return (
    <div className={'onb-check ' + state}>
      <span className="onb-check-dot" />
      <span>{state === 'checking' ? checkingText : state === 'ok' ? ok : bad}</span>
    </div>
  );
}

function Code({ children, inline }: { children: any; inline?: boolean }) {
  if (inline) return <code className="onb-code-inline">{children}</code>;
  return (
    <pre className="onb-code">
      <code>{children}</code>
    </pre>
  );
}

function IconPc() {
  return (
    <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
      <rect x="3" y="4" width="18" height="12" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 20h8M12 16v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function IconGateway() {
  return (
    <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
      <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="m3 12 9 4.5L21 12" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

function IconServer() {
  return (
    <svg viewBox="0 0 24 24" fill="none" width="22" height="22">
      <rect x="4" y="4" width="16" height="6" rx="1.4" stroke="currentColor" strokeWidth="1.6" />
      <rect x="4" y="14" width="16" height="6" rx="1.4" stroke="currentColor" strokeWidth="1.6" />
      <path d="M7.5 7h.01M7.5 17h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
