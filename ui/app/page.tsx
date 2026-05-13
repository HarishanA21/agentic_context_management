import Link from 'next/link'

export default function Landing() {
  return (
    <div className="min-h-screen bg-ink-50 text-fog-100 antialiased">
      <Nav />
      <Hero />
      <ProductPreview />
      <Features />
      <HowItWorks />
      <UseCases />
      <FAQ />
      <CTA />
      <Footer />
    </div>
  )
}

/* ─────────────────────────────────── Nav ─────────────────────────────────── */

function Nav() {
  return (
    <header className="sticky top-0 z-40 backdrop-blur-md bg-ink-50/70 border-b border-line">
      <div className="mx-auto max-w-7xl px-6 h-14 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2 group">
          <Glyph />
          <span className="text-[15px] tracking-tight font-medium">agent</span>
        </Link>

        <nav className="hidden md:flex items-center gap-7 text-sm text-fog-200">
          <a href="#features" className="hover:text-fog-50 transition">
            Product
          </a>
          <a href="#how" className="hover:text-fog-50 transition">
            How it works
          </a>
          <a href="#use-cases" className="hover:text-fog-50 transition">
            Use cases
          </a>
          <a href="#faq" className="hover:text-fog-50 transition">
            FAQ
          </a>
          <a
            href="https://github.com"
            target="_blank"
            rel="noreferrer"
            className="hover:text-fog-50 transition"
          >
            Docs
          </a>
        </nav>

        <div className="flex items-center gap-2">
          <Link
            href="/login"
            className="hidden sm:inline-flex text-sm text-fog-200 hover:text-fog-50 px-3 py-1.5 transition"
          >
            Sign in
          </Link>
          <Link href="/login" className="btn-white text-sm">
            Get started
            <ArrowRight />
          </Link>
        </div>
      </div>
    </header>
  )
}

/* ────────────────────────────────── Hero ─────────────────────────────────── */

function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div className="pointer-events-none absolute inset-0 bg-radial-fade" />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-[600px] opacity-[0.06] bg-grid-faint bg-grid-32 [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]"
      />
      <div className="relative mx-auto max-w-5xl px-6 pt-24 pb-16 sm:pt-32 sm:pb-24 text-center">
        <div className="inline-flex items-center gap-2 chip mb-7">
          <span className="dot bg-emerald-400 animate-pulse-soft" />
          Now in private preview
        </div>

        <h1 className="serif text-5xl sm:text-7xl leading-[1.02] tracking-tighter text-fog-50">
          An agent that keeps
          <br />
          <span className="italic text-fog-200">your context.</span>
        </h1>

        <p className="mt-7 mx-auto max-w-2xl text-fog-200 text-lg leading-relaxed">
          Persistent chat sessions, threaded conversations, and tool-aware
          reasoning. Build, ask, and ship — without losing your place.
        </p>

        <div className="mt-10 flex items-center justify-center gap-3">
          <Link href="/login" className="btn-white">
            Start building
            <ArrowRight />
          </Link>
          <a href="#features" className="btn-ghost">
            See how it works
          </a>
        </div>

        <p className="mt-8 text-xs text-fog-400">
          Free to try · No credit card · Bring your own model
        </p>
      </div>
    </section>
  )
}

/* ───────────────────────────── Product preview ───────────────────────────── */

function ProductPreview() {
  return (
    <section className="relative px-6 pb-24">
      <div className="mx-auto max-w-6xl">
        <div className="surface shadow-glow overflow-hidden">
          {/* Window chrome */}
          <div className="flex items-center gap-2 px-4 py-3 border-b border-line">
            <span className="w-2.5 h-2.5 rounded-full bg-ink-600" />
            <span className="w-2.5 h-2.5 rounded-full bg-ink-600" />
            <span className="w-2.5 h-2.5 rounded-full bg-ink-600" />
            <div className="ml-3 text-xs text-fog-400 font-mono">
              agent.app/app
            </div>
          </div>

          <div className="grid grid-cols-12 min-h-[460px]">
            {/* Sidebar mock */}
            <div className="col-span-4 lg:col-span-3 border-r border-line p-3 bg-ink-100/60">
              <div className="text-[11px] uppercase tracking-widest text-fog-400 px-2 mb-3">
                Tasks
              </div>
              {previewTasks.map((t, i) => (
                <div
                  key={i}
                  className={`flex items-start gap-2 px-2 py-2 rounded-lg mb-1 ${
                    i === 0 ? 'bg-soft/5' : 'hover:bg-soft/[0.03]'
                  }`}
                >
                  <span
                    className={`mt-1.5 dot ${t.color}`}
                    aria-hidden
                  />
                  <div className="min-w-0">
                    <div className="text-sm text-fog-50 truncate">{t.title}</div>
                    <div className="text-[11px] text-fog-400 truncate">
                      {t.sub}
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {/* Conversation mock */}
            <div className="col-span-8 lg:col-span-9 p-6 sm:p-8 flex flex-col">
              <div className="text-[11px] uppercase tracking-widest text-fog-400 mb-4">
                Migrate auth provider
              </div>

              <div className="flex justify-end mb-4">
                <div className="max-w-md text-sm bg-soft/[0.06] border border-line rounded-2xl rounded-br-md px-4 py-2.5">
                  Walk me through migrating from custom JWT to Supabase auth in
                  this repo.
                </div>
              </div>

              <div className="text-[15px] leading-7 text-fog-100 max-w-2xl space-y-3">
                <p>
                  Here's a clean migration path for this codebase. You can do
                  this in three passes:
                </p>
                <ol className="list-decimal pl-5 space-y-1 text-fog-200">
                  <li>Replace the JWT middleware with a Supabase JWKS check.</li>
                  <li>
                    Move user creation flow to <code className="font-mono text-xs px-1.5 py-0.5 bg-soft/[0.06] rounded">supabase.auth.signUp</code>.
                  </li>
                  <li>Drop the legacy <code className="font-mono text-xs px-1.5 py-0.5 bg-soft/[0.06] rounded">users</code> table once backfill is verified.</li>
                </ol>
                <p className="text-fog-300 text-sm">
                  I'll run a search to find all current JWT references…
                </p>
              </div>

              <div className="mt-auto pt-6">
                <div className="rounded-2xl border border-line bg-ink-100/80 p-3 flex items-center gap-3">
                  <div className="text-sm text-fog-300 flex-1">
                    Send a message…
                  </div>
                  <span className="chip">glm-4.5-air</span>
                  <button className="rounded-full bg-accent text-ink-50 w-8 h-8 flex items-center justify-center">
                    <ArrowUp />
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

const previewTasks = [
  { title: 'Migrate auth provider', sub: 'Active · 12 messages', color: 'bg-emerald-400' },
  { title: 'Refactor billing module', sub: 'Idle · 3 messages', color: 'bg-fog-400' },
  { title: 'Draft API spec for v2', sub: 'Idle · 7 messages', color: 'bg-fog-400' },
  { title: 'Investigate p99 latency', sub: 'Done · 22 messages', color: 'bg-fog-500' },
  { title: 'Onboarding email flow', sub: 'Done · 5 messages', color: 'bg-fog-500' },
]

/* ───────────────────────────────── Features ──────────────────────────────── */

function Features() {
  return (
    <section id="features" className="py-24 px-6 border-t border-line">
      <div className="mx-auto max-w-6xl">
        <div className="max-w-2xl mb-16">
          <p className="text-xs uppercase tracking-[0.2em] text-fog-400 mb-3">
            Built for serious work
          </p>
          <h2 className="serif text-4xl sm:text-5xl leading-[1.05] tracking-tighter text-fog-50">
            Context is the product.
          </h2>
          <p className="mt-4 text-fog-200 text-lg">
            Most chat tools forget. This one remembers — across sessions,
            threads, and tools, with state you can return to days later.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-px bg-line rounded-2xl overflow-hidden border border-line">
          {features.map((f) => (
            <div key={f.title} className="bg-ink-50 p-7">
              <div className="w-9 h-9 rounded-lg border border-line bg-soft/[0.03] flex items-center justify-center mb-5 text-fog-100">
                {f.icon}
              </div>
              <h3 className="text-base font-medium text-fog-50 mb-1.5">
                {f.title}
              </h3>
              <p className="text-sm text-fog-300 leading-relaxed">{f.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

const features = [
  {
    title: 'Persistent sessions',
    body: 'Group threads under projects. Pick up exactly where you left off — even days later.',
    icon: <IconLayers />,
  },
  {
    title: 'Tool-aware agent',
    body: 'Plug in tools the agent can call. Calculator, weather, your own — extensible by design.',
    icon: <IconTool />,
  },
  {
    title: 'Bring your own model',
    body: 'OpenRouter under the hood. Swap models per task, mix free and paid tiers.',
    icon: <IconCpu />,
  },
  {
    title: 'Per-user isolation',
    body: 'Postgres-level row security plus checkpoint scoping — your context never leaks.',
    icon: <IconShield />,
  },
  {
    title: 'Threaded conversations',
    body: 'Branch a project into focused threads. No more "where did we leave off?" spirals.',
    icon: <IconBranch />,
  },
  {
    title: 'Markdown-native',
    body: 'Code blocks, tables, GFM out of the box — built for technical work.',
    icon: <IconCode />,
  },
]

/* ─────────────────────────────── How it works ────────────────────────────── */

function HowItWorks() {
  return (
    <section id="how" className="py-24 px-6 border-t border-line">
      <div className="mx-auto max-w-6xl">
        <div className="max-w-2xl mb-16">
          <p className="text-xs uppercase tracking-[0.2em] text-fog-400 mb-3">
            How it works
          </p>
          <h2 className="serif text-4xl sm:text-5xl leading-[1.05] tracking-tighter text-fog-50">
            Four steps to flow.
          </h2>
        </div>

        <div className="grid md:grid-cols-2 gap-x-12 gap-y-10">
          {steps.map((s, i) => (
            <div key={s.title} className="flex gap-5">
              <div className="serif text-4xl text-fog-400 leading-none w-12 shrink-0">
                {String(i + 1).padStart(2, '0')}
              </div>
              <div>
                <h3 className="text-lg text-fog-50 mb-1">{s.title}</h3>
                <p className="text-fog-300 leading-relaxed">{s.body}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

const steps = [
  {
    title: 'Sign in',
    body: 'Email + password through Supabase. Your session is yours alone.',
  },
  {
    title: 'Create a project',
    body: 'A project groups related threads. Name it after the work, not the day.',
  },
  {
    title: 'Start a thread',
    body: 'Each thread is its own conversation, with its own LangGraph checkpoint.',
  },
  {
    title: 'Ship',
    body: "Ask. Iterate. Tools fire when they're useful. Come back tomorrow — context intact.",
  },
]

/* ───────────────────────────────── Use cases ─────────────────────────────── */

function UseCases() {
  return (
    <section id="use-cases" className="py-24 px-6 border-t border-line">
      <div className="mx-auto max-w-6xl">
        <div className="max-w-2xl mb-16">
          <p className="text-xs uppercase tracking-[0.2em] text-fog-400 mb-3">
            Use cases
          </p>
          <h2 className="serif text-4xl sm:text-5xl leading-[1.05] tracking-tighter text-fog-50">
            One agent. Many surfaces.
          </h2>
        </div>

        <div className="grid lg:grid-cols-3 gap-5">
          {useCases.map((u) => (
            <div
              key={u.title}
              className="surface p-7 hover:bg-soft/[0.02] transition-colors"
            >
              <div className="text-xs text-fog-400 mb-2">{u.tag}</div>
              <h3 className="serif text-2xl text-fog-50 tracking-tight mb-3">
                {u.title}
              </h3>
              <p className="text-fog-300 leading-relaxed text-sm">{u.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

const useCases = [
  {
    tag: 'Engineering',
    title: 'A second brain for your repo',
    body: 'Ask deep questions across a long-running thread. The agent stays in context as the codebase evolves.',
  },
  {
    tag: 'Research',
    title: 'Long-form, grounded thinking',
    body: 'Build up a research thread over days. Tools fetch what you need; the agent ties it together.',
  },
  {
    tag: 'Operations',
    title: 'Runbooks that talk back',
    body: 'Codify procedures in threads. Run them on demand with structured tool calls and clean handoffs.',
  },
]

/* ────────────────────────────────────── FAQ ──────────────────────────────── */

function FAQ() {
  return (
    <section id="faq" className="py-24 px-6 border-t border-line">
      <div className="mx-auto max-w-3xl">
        <p className="text-xs uppercase tracking-[0.2em] text-fog-400 mb-3 text-center">
          FAQ
        </p>
        <h2 className="serif text-4xl sm:text-5xl leading-[1.05] tracking-tighter text-fog-50 text-center mb-12">
          Questions, answered.
        </h2>

        <div className="divide-y divide-line border-y border-line">
          {faq.map((q) => (
            <details key={q.q} className="group py-5">
              <summary className="cursor-pointer list-none flex items-center justify-between gap-6">
                <span className="text-fog-50 text-base">{q.q}</span>
                <span className="text-fog-400 text-2xl leading-none transition-transform group-open:rotate-45">
                  +
                </span>
              </summary>
              <p className="mt-3 text-fog-300 leading-relaxed text-[15px]">
                {q.a}
              </p>
            </details>
          ))}
        </div>
      </div>
    </section>
  )
}

const faq = [
  {
    q: 'Which models can I use?',
    a: 'Anything OpenRouter supports. The default is z-ai/glm-4.5-air:free, but you can swap by setting the model in your backend config.',
  },
  {
    q: 'Where is my data stored?',
    a: 'In your own Supabase project. Sessions, threads, and messages live in Postgres with row-level security; LangGraph checkpoints live in their own scoped tables.',
  },
  {
    q: 'Can other users see my threads?',
    a: 'No. Threads are scoped per user via RLS, and LangGraph state is keyed by user_id:session_id so checkpoints can never cross users.',
  },
  {
    q: 'Is there an API?',
    a: 'Yes — the FastAPI backend exposes /api/sessions, /api/threads, /api/chat, and /api/history. JWT auth via Supabase.',
  },
  {
    q: 'How do I add a tool?',
    a: 'Drop a @tool-decorated function into Tools/, register it in Tools/__init__.py, and restart the backend. The agent picks it up automatically.',
  },
]

/* ─────────────────────────────────── CTA ─────────────────────────────────── */

function CTA() {
  return (
    <section className="py-28 px-6 border-t border-line">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="serif text-5xl sm:text-6xl leading-[1.02] tracking-tighter text-fog-50">
          Ready when you are.
        </h2>
        <p className="mt-5 text-fog-200 text-lg">
          Spin up a project, start a thread, and let the agent do the rest.
        </p>
        <div className="mt-9 flex items-center justify-center gap-3">
          <Link href="/login" className="btn-white">
            Get started
            <ArrowRight />
          </Link>
          <a href="#features" className="btn-ghost">
            Learn more
          </a>
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────────────── Footer ────────────────────────────────── */

function Footer() {
  return (
    <footer className="border-t border-line py-12 px-6">
      <div className="mx-auto max-w-6xl flex flex-col md:flex-row md:items-center md:justify-between gap-6">
        <div className="flex items-center gap-2">
          <Glyph />
          <span className="text-sm tracking-tight">agent</span>
          <span className="text-fog-400 text-sm ml-3">
            © {new Date().getFullYear()}
          </span>
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-fog-300">
          <a href="#features" className="hover:text-fog-50">Product</a>
          <a href="#how" className="hover:text-fog-50">How it works</a>
          <a href="#faq" className="hover:text-fog-50">FAQ</a>
          <Link href="/login" className="hover:text-fog-50">Sign in</Link>
        </div>
      </div>
    </footer>
  )
}

/* ────────────────────────────── Icons / glyphs ───────────────────────────── */

function Glyph() {
  return (
    <span className="relative w-6 h-6 inline-flex items-center justify-center">
      <span className="absolute inset-0 rounded-md bg-soft/10 group-hover:bg-soft/20 transition" />
      <span className="relative w-2 h-2 rounded-sm bg-accent" />
    </span>
  )
}

function ArrowRight() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <path d="M5 12h14M13 5l7 7-7 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function ArrowUp() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function IconLayers() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <path d="M12 3l9 5-9 5-9-5 9-5zM3 14l9 5 9-5M3 19l9 5 9-5" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  )
}
function IconTool() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <path d="M14.7 6.3a4 4 0 015.7 5.7l-9.4 9.4a2 2 0 01-2.8 0l-2.6-2.6a2 2 0 010-2.8l9.1-9.7zM4 20l3-3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function IconCpu() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <rect x="6" y="6" width="12" height="12" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <rect x="9" y="9" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.6" />
      <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}
function IconShield() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  )
}
function IconBranch() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <circle cx="6" cy="6" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="6" cy="18" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="18" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6 8.2v7.6M8.2 6h4.6A4 4 0 0117 10v.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}
function IconCode() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <path d="M9 7l-5 5 5 5M15 7l5 5-5 5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
