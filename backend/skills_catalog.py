"""Built-in "skills" catalog + system-prompt injection helpers.

A *skill* is a named bundle of instructions the user can toggle on for the
agent. When enabled, the skill's instruction text is appended to the agent's
system prompt (see ``_get_agent_for_request`` in ``api.py``). This mirrors how
claude.ai surfaces skills behind the composer "+" menu, scoped down to the
inject-instructions model: no executable bundles, just guidance the model
folds into its behaviour.

Two kinds of skill exist:

  * **Catalog skills** — defined here in code (``CATALOG``). Stable ``slug``
    identity; their instruction text always comes from this file, so editing a
    catalog skill here auto-deploys to every user who enabled it. A user only
    gets a ``skills`` row for a catalog skill once they toggle it (the row just
    tracks ``enabled``); content is never copied into the row.
  * **Custom skills** — user-authored rows (``is_custom = true``) carrying their
    own name/description/instructions.

The DB schema lives in ``db/init.sql`` and the idempotent lifespan migration in
``api.py``. CRUD + the merged listing is in ``routes_skills.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ── Built-in catalog ─────────────────────────────────────────────────────────
# Each entry powers a card in the "Directory" modal and, when enabled, an
# injected system-prompt block. Fields:
#   slug         — stable id.
#   name         — shown as "/name" on the card.
#   publisher    — vendor label on the card.
#   downloads    — cosmetic install-count string (display only).
#   description  — tells the model *when* to use the skill AND is the card blurb.
#   icon         — UI glyph hint ("web" | "doc" | "spark").
#   instructions — the body folded into the system prompt while the skill is on.
CATALOG: List[Dict[str, str]] = [
    {
        "slug": "web-artifacts-builder",
        "name": "web-artifacts-builder",
        "publisher": "Anthropic",
        "downloads": "600.2K",
        "description": (
            "Build interactive, self-contained web artifacts — tools, "
            "dashboards, widgets, games, calculators, demos — using modern "
            "frontend techniques. Use when the user asks for an interactive UI or "
            "app (something that responds to input), not just static text."
        ),
        "icon": "web",
        "instructions": (
            "## Skill: web-artifacts-builder\n"
            "Build interactive web artifacts (tools, dashboards, widgets, games, "
            "calculators, demos) as a SINGLE self-contained HTML file that opens "
            "and runs by double-clicking — no build step, no bundler, no npm.\n"
            "\n"
            "Follow this process every time:\n"
            "\n"
            "1. SPEC — In one line, restate what the artifact does, its core "
            "interaction, and the inputs/outputs. List the few features that "
            "matter; do not over-scope.\n"
            "\n"
            "2. STRUCTURE — One `.html` file with three inline sections:\n"
            "   - semantic HTML markup (use <header>/<main>/<button>/<label>, real "
            "form controls);\n"
            "   - all CSS in one <style> tag;\n"
            "   - all JS in one <script> tag at the end of <body>.\n"
            "   No external local files. Only pull a CDN library (via <script src> "
            "or <link>) when it genuinely helps, and PIN the version. Prefer "
            "vanilla JS for anything small.\n"
            "\n"
            "3. STATE — Keep a single explicit state object and a render() that "
            "redraws the UI from state on each change (one-way data flow). Wire "
            "events with addEventListener; never rebuild logic in multiple places. "
            "Persist to localStorage only if the feature needs it.\n"
            "\n"
            "4. QUALITY — Make it responsive (works at phone and desktop widths) "
            "and accessible (labelled inputs, keyboard usable, focus states, "
            "sufficient contrast). Handle empty, loading, and error states. "
            "Validate user input; never let the UI break on bad input.\n"
            "\n"
            "5. POLISH — Clean, modern visual design: a small consistent spacing "
            "scale, a restrained palette, readable type, and light transitions. "
            "Respect `prefers-color-scheme` when reasonable.\n"
            "\n"
            "6. DELIVER — Save with write_project_file using a descriptive "
            "`.html` filename (e.g. `expense-splitter.html`). Then tell the user "
            "how to open it and give a one-line tour of the main interaction.\n"
            "\n"
            "Keep it genuinely self-contained: if you reference a font or library, "
            "it must load from a CDN or degrade gracefully offline — never from a "
            "local path that won't exist on the user's machine."
        ),
    },
    {
        "slug": "skill-creator",
        "name": "skill-creator",
        "publisher": "Anthropic",
        "downloads": "84.6K",
        "description": (
            "Help the user author a new skill, or improve an existing one. Use "
            "when the user wants to create, design, or refine a skill — it "
            "produces a ready-to-paste Name, Description, and Instructions for the "
            "'Add skill' form."
        ),
        "icon": "spark",
        "instructions": (
            "## Skill: skill-creator\n"
            "Help the user author a high-quality skill for this agent. A skill is "
            "a Name, a Description (tells the agent WHEN to use it), and "
            "Instructions (tells the agent HOW to behave while it's on). It is "
            "instruction-only — it steers the agent and relies on the agent's "
            "existing tools; it does not add new code.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. UNDERSTAND — Ask what task the skill should make the agent better "
            "at, and for one or two concrete example requests that should trigger "
            "it. If the user already gave enough, skip ahead — don't over-"
            "interrogate.\n"
            "\n"
            "2. NAME — Propose a short, lowercase, kebab-case name that reads like "
            "a capability (e.g. `release-notes-writer`, `sql-reviewer`).\n"
            "\n"
            "3. DESCRIPTION — Write 1–2 sentences focused on the TRIGGER: the "
            "situations in which the agent should reach for this skill. No 'how' "
            "here — the agent uses this to decide when to apply the skill.\n"
            "\n"
            "4. INSTRUCTIONS — Write the behaviour the agent should follow. Make "
            "them:\n"
            "   - actionable and specific (numbered steps or tight bullets);\n"
            "   - explicit about the OUTPUT format and any hard rules / things to "
            "avoid;\n"
            "   - clear about which existing tools to use (e.g. read_project_file, "
            "write_project_file, run_shell) when the task needs them;\n"
            "   - self-contained: assume only this text is injected, so restate "
            "any context the agent needs.\n"
            "   Aim for focused and usable — long enough to be unambiguous, short "
            "enough to stay sharp.\n"
            "\n"
            "5. OUTPUT — Present the result as three clearly labelled blocks "
            "exactly: **Name**, **Description**, **Instructions** — so the user "
            "can paste them straight into the 'Add skill' form. Then offer one "
            "round of refinement.\n"
            "\n"
            "Good skills are narrow and opinionated. If a request is really two "
            "skills, say so and propose splitting it."
        ),
    },
    {
        "slug": "canvas-design",
        "name": "canvas-design",
        "publisher": "Anthropic",
        "downloads": "962.6K",
        "description": (
            "Create beautiful, original visual art driven by a design philosophy "
            "— posters, covers, social graphics, one-page layouts, diagrams. Use "
            "when the user asks to design a poster, piece of art, cover, or other "
            "static visual piece. Always create original work; never copy a "
            "specific artist's or brand's design."
        ),
        "icon": "doc",
        "instructions": (
            "## Skill: canvas-design\n"
            "Create beautiful, ORIGINAL static visual pieces (posters, covers, "
            "social graphics, one-page layouts, simple diagrams). The work is "
            "driven by a chosen design philosophy that is then expressed visually. "
            "Never copy a specific living artist's or brand's work — invent "
            "original compositions.\n"
            "\n"
            "Follow this process every time:\n"
            "\n"
            "1. BRIEF — Restate in one line what is being made, its purpose, the "
            "audience, and the output size (e.g. A4 poster, 1080×1080 social, "
            "16:9 slide). If the size is unstated, pick a sensible one and say so.\n"
            "\n"
            "2. DESIGN PHILOSOPHY — Choose ONE aesthetic direction that fits the "
            "brief and name it explicitly, with 2–3 guiding principles. Examples: "
            "Swiss/International (grid, Helvetica, lots of white space), Bauhaus "
            "(primary colours, geometric), brutalist (raw, high-contrast, mono), "
            "editorial/magazine, art-deco, minimalist, organic/botanical, retro-"
            "print. The philosophy drives every later decision.\n"
            "\n"
            "3. VISUAL LANGUAGE — Derive concrete tokens from the philosophy:\n"
            "   - Palette: 2–4 colours + neutrals, given as hex. Assign roles "
            "(background, primary, accent, text). Ensure strong text contrast.\n"
            "   - Type: ONE display/heading font + ONE text font. Use real, freely "
            "available fonts (Google Fonts) and load them with a <link>; fall back "
            "to system fonts. Define a clear type scale.\n"
            "   - Grid & spacing: pick a column grid and a consistent spacing scale; "
            "align everything to it.\n"
            "   - Motifs: simple shapes/rules/textures consistent with the "
            "philosophy (drawn as CSS/SVG — do not embed copyrighted images).\n"
            "\n"
            "4. COMPOSE with strong fundamentals: a single clear focal point; "
            "hierarchy via scale and weight (not colour alone); generous, "
            "consistent whitespace; deliberate alignment; high contrast for the "
            "key message.\n"
            "\n"
            "5. OUTPUT — Produce ONE self-contained artifact and save it with "
            "write_project_file:\n"
            "   - Default to a single .html file (inline CSS, fonts via Google "
            "Fonts <link>, motifs as inline SVG/CSS) sized exactly to the target "
            "(set the page/canvas dimensions; make it print-ready).\n"
            "   - Use a descriptive filename, e.g. `poster-jazz-night.html`.\n"
            "   - Tell the user how to export to PNG/PDF (open in a browser → Print "
            "→ Save as PDF, or screenshot at the artboard size). If a project "
            "workspace with run_shell is available and they want a rendered .png/"
            ".pdf, you may generate it there.\n"
            "\n"
            "6. EXPLAIN briefly: name the design philosophy, the palette, the font "
            "pairing, and the one idea the composition leads with."
        ),
    },
    {
        "slug": "mcp-builder",
        "name": "mcp-builder",
        "publisher": "Anthropic",
        "downloads": "490K",
        "description": (
            "Build a high-quality MCP (Model Context Protocol) server that lets "
            "LLM clients call your tools. Use when the user wants to create, "
            "scaffold, or improve an MCP server, or expose an API/system as MCP "
            "tools."
        ),
        "icon": "spark",
        "instructions": (
            "## Skill: mcp-builder\n"
            "Help the user build a clean, spec-compliant MCP (Model Context "
            "Protocol) server that exposes tools an LLM client can call.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. SCOPE — Restate what the server exposes (which actions/data) and "
            "who calls it. List the concrete tools it should offer; keep each tool "
            "single-purpose.\n"
            "\n"
            "2. TRANSPORT — Choose and justify one: stdio for a local/desktop "
            "client, or streamable HTTP for a remote/multi-client server. Mention "
            "the trade-off in one line.\n"
            "\n"
            "3. STACK — Default to the official MCP SDK (Python `mcp` or "
            "TypeScript `@modelcontextprotocol/sdk`) unless the user prefers "
            "otherwise. Match the language of their existing code when known.\n"
            "\n"
            "4. TOOLS — For each tool: a narrow name, a one-line description the "
            "model can route on, a precise input schema (JSON Schema / typed args "
            "with required fields and constraints), and a STRUCTURED result. "
            "Validate every input; on failure return a clear typed error, never a "
            "raw stack trace. Never overload one tool with many unrelated modes.\n"
            "\n"
            "5. SAFETY — Keep secrets in environment variables (never hard-code). "
            "Apply least privilege, time out external calls, and guard against "
            "obvious injection/abuse in any tool that runs commands or queries.\n"
            "\n"
            "6. DELIVER — Provide runnable code (saved with write_project_file), a "
            "minimal manifest/config, a `requirements`/`package.json`, and a "
            "one-command way to run and smoke-test it locally. Finish with the "
            "exact client config snippet to register the server.\n"
            "\n"
            "Prefer a small, correct, well-documented server over a broad one. "
            "Call out anything that needs the user's credentials or environment."
        ),
    },
    {
        "slug": "theme-factory",
        "name": "theme-factory",
        "publisher": "Anthropic",
        "downloads": "480.4K",
        "description": (
            "Apply a consistent, reusable theme across an artifact — slides, "
            "docs, reports, HTML pages. Use when the user wants a cohesive look "
            "or asks to theme / restyle / re-skin something so it can be changed "
            "in one place."
        ),
        "icon": "web",
        "instructions": (
            "## Skill: theme-factory\n"
            "Give an artifact a cohesive, reusable theme so its entire look can be "
            "changed by editing one block. Works on slides, docs, reports, and "
            "HTML pages.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. READ THE BRIEF — Note the artifact type, the mood/keywords the "
            "user wants (e.g. 'calm, editorial' or 'bold, techy'), and any fixed "
            "constraints (brand colour, required font). If a theme already exists, "
            "read the file first with read_project_file before restyling.\n"
            "\n"
            "2. DEFINE TOKENS — Declare the theme as design tokens in ONE place "
            "(CSS custom properties under :root). Cover: colour ROLES (background, "
            "surface, text, muted, primary, accent, border), a type scale (font "
            "families + step sizes), spacing scale, radius, and shadow. Name tokens "
            "by role, never by raw value (`--color-primary`, not `--blue`).\n"
            "\n"
            "3. DERIVE EVERYTHING — Style every element from the tokens only — no "
            "hard-coded hex or pixel values scattered in components. This is what "
            "makes the artifact re-themeable from the token block.\n"
            "\n"
            "4. VARIANTS — Provide a light and a dark variant (e.g. a "
            "`[data-theme='dark']` override of the tokens) and respect "
            "`prefers-color-scheme`. Check text/background contrast meets WCAG AA.\n"
            "\n"
            "5. DELIVER — Keep it self-contained and save with write_project_file. "
            "At the top, document the token names and show a one-line example of "
            "changing the theme (e.g. 'swap --color-primary to re-skin'). If "
            "applying to an existing artifact, preserve its content and only "
            "change styling."
        ),
    },
    {
        "slug": "brand-guidelines",
        "name": "brand-guidelines",
        "publisher": "Anthropic",
        "downloads": "440.9K",
        "description": (
            "Apply a brand's official colours, typography, and voice to any "
            "artifact so it looks on-brand. Use when the user wants something to "
            "match a brand, or provides brand guidelines / a logo / brand colours "
            "to follow."
        ),
        "icon": "doc",
        "instructions": (
            "## Skill: brand-guidelines\n"
            "Make every artifact look and sound on-brand by enforcing a brand "
            "system consistently.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. GATHER THE BRAND — Establish the brand's primary/secondary "
            "colours (hex), typefaces, logo, and tone of voice. If the user "
            "supplied a brand file, read it with read_project_file. If anything is "
            "unknown, propose a tasteful default, clearly flag it as an "
            "assumption, and proceed.\n"
            "\n"
            "2. COLOUR — Apply colours by ROLE (brand, accent, surface, text, "
            "muted) defined once as tokens — never scatter raw hex inline. Use "
            "brand colours for emphasis, not everywhere; keep large areas neutral.\n"
            "\n"
            "3. TYPOGRAPHY — Use the brand typefaces (with sensible web fallbacks) "
            "in a clear type scale. Keep heading/body pairing, weight, and "
            "capitalisation consistent with the brand.\n"
            "\n"
            "4. LOGO & SPACE — If a logo is used, respect minimum clear-space, "
            "don't stretch or recolour it, and place it consistently. Maintain "
            "generous, consistent spacing throughout.\n"
            "\n"
            "5. VOICE — Match the brand's tone in any copy you write (e.g. formal "
            "vs. playful); keep terminology and capitalisation of product names "
            "consistent.\n"
            "\n"
            "6. ACCESSIBILITY & CONFLICTS — Ensure text/background contrast meets "
            "WCAG AA even with brand colours. If a request would violate the "
            "guidelines (e.g. an off-brand colour, illegible combo), say so and "
            "offer an on-brand alternative instead of silently breaking the rules. "
            "Save the result with write_project_file."
        ),
    },
    {
        "slug": "doc-coauthoring",
        "name": "doc-coauthoring",
        "publisher": "Anthropic",
        "downloads": "318.7K",
        "description": (
            "Co-write and edit long-form documents — proposals, specs, articles, "
            "READMEs — with structure, a consistent voice, and careful revisions. "
            "Use when the user wants to draft, expand, or iteratively edit a "
            "multi-section document together."
        ),
        "icon": "doc",
        "instructions": (
            "## Skill: doc-coauthoring\n"
            "Co-write long-form documents (proposals, specs, articles, READMEs) "
            "with the user, working like a careful editor — not a one-shot "
            "generator.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. OUTLINE FIRST — For a new document, propose a section outline and "
            "confirm it before drafting the full text. For an existing document, "
            "read it with read_project_file before changing anything.\n"
            "\n"
            "2. DRAFT IN VOICE — Write in the user's voice and reading level. Keep "
            "terminology, tense, and point of view consistent throughout. Prefer "
            "clear, concrete prose over padding; every section should earn its "
            "place.\n"
            "\n"
            "3. EDIT SURGICALLY — When revising, make ONLY the change requested. Do "
            "not silently rewrite untouched sections. After editing, give a short "
            "summary of exactly what changed and why.\n"
            "\n"
            "4. STRUCTURE — Maintain clean Markdown: a clear heading hierarchy, "
            "lists and tables where they aid scanning, and short paragraphs. Keep "
            "one idea per section.\n"
            "\n"
            "5. TRACK OPEN ITEMS — Where information is missing or a decision is "
            "needed, leave an inline `TODO:` or `[NEEDS INPUT: …]` marker rather "
            "than inventing facts, so nothing is lost.\n"
            "\n"
            "6. SAVE — Write the document to a `.md` file with write_project_file "
            "using a descriptive name, and on each round update the same file "
            "rather than spawning copies. Note what you'd tackle next."
        ),
    },
    {
        "slug": "internal-comms",
        "name": "internal-comms",
        "publisher": "Anthropic",
        "downloads": "402.1K",
        "description": (
            "Draft clear internal communications — announcements, status updates, "
            "incident notes, decision summaries. Use when the user needs to "
            "communicate something to a team or organisation in a skimmable, "
            "well-targeted message."
        ),
        "icon": "spark",
        "instructions": (
            "## Skill: internal-comms\n"
            "Draft internal communications (announcements, status updates, "
            "incident notes, decision summaries) that are clear, skimmable, and "
            "tuned to the audience.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. AUDIENCE & CHANNEL — Identify who reads this and where (exec "
            "update, all-hands announcement, team Slack note, email). That sets "
            "the length and tone. If unclear, ask once or pick the most likely and "
            "say which.\n"
            "\n"
            "2. BLUF — Lead with the bottom line in the first sentence: what is "
            "happening and why it matters. The reader should get the point even if "
            "they stop after line one.\n"
            "\n"
            "3. THE ESSENTIALS — Cover, briefly: WHAT is changing, WHO is "
            "affected, WHEN (dates), and WHO owns it. Use bold mini-labels and "
            "bullets so it scans in seconds. Cut anything that isn't decision-"
            "relevant.\n"
            "\n"
            "4. TONE — Match the register to the audience and keep it human: plain "
            "language, no internal jargon or acronyms without a gloss. Be honest "
            "and direct, especially for bad news — don't bury it.\n"
            "\n"
            "5. CALL TO ACTION — End with exactly what you want the reader to do "
            "(if anything) and where to ask questions or get more detail.\n"
            "\n"
            "6. DELIVER — Provide a clear subject/title line plus the body. Offer "
            "length variants when useful (e.g. a one-line summary + the full "
            "note). Save to a file with write_project_file only if the user asks."
        ),
    },
    {
        "slug": "report-writer",
        "name": "report-writer",
        "publisher": "Anthropic",
        "downloads": "210.5K",
        "description": (
            "Turn source material or findings into a well-structured Markdown "
            "report — title, executive summary, sections, findings, conclusion. "
            "Use when the user asks for a report, write-up, brief, analysis, or "
            "summary document."
        ),
        "icon": "doc",
        "instructions": (
            "## Skill: report-writer\n"
            "Turn source material or findings into a clear, well-structured "
            "Markdown report a busy reader can act on.\n"
            "\n"
            "Follow this process:\n"
            "\n"
            "1. GATHER — Work from the material provided. If the report is about "
            "project files, read them first with read_project_file; do not "
            "describe files you haven't read. If key information is missing, note "
            "the gap rather than inventing it.\n"
            "\n"
            "2. STRUCTURE — Use this skeleton, adapting section names to the "
            "topic:\n"
            "   - H1 title.\n"
            "   - Executive summary: 2–4 sentences with the key takeaway and "
            "recommendation up front.\n"
            "   - H2 body sections with descriptive headings, ordered most-"
            "important first.\n"
            "   - A short 'Conclusion' or 'Next steps' section.\n"
            "\n"
            "3. WRITE FOR SKIM — Keep paragraphs tight (≤4 lines). Use bullet "
            "lists for findings and Markdown tables for comparisons or metrics. "
            "Bold the few phrases that carry the decision.\n"
            "\n"
            "4. BE SPECIFIC & HONEST — Use concrete numbers, names, and examples "
            "from the source; attribute claims to where they came from. Never pad "
            "with filler or overstate certainty; flag assumptions and open "
            "questions explicitly.\n"
            "\n"
            "5. DELIVER — Save to a descriptive `.md` file with write_project_file "
            "when the report is substantial, and give the user a one-line summary "
            "of what's inside."
        ),
    },
]

_CATALOG_BY_SLUG: Dict[str, Dict[str, str]] = {e["slug"]: e for e in CATALOG}


def catalog_entry(slug: str) -> Optional[Dict[str, str]]:
    """Return the catalog entry for ``slug`` (or ``None`` if unknown)."""
    return _CATALOG_BY_SLUG.get(slug)


def resolve_enabled_skill(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Turn an enabled ``skills`` row into {name, description, instructions}.

    Catalog rows pull their content from ``CATALOG`` (so code edits win);
    custom rows carry their own. Returns ``None`` if a catalog row points at a
    slug we no longer ship (e.g. a skill was removed between releases).
    """
    if row.get("is_custom"):
        return {
            "name": row.get("name") or "skill",
            "description": row.get("description") or "",
            "instructions": row.get("instructions") or "",
        }
    entry = catalog_entry(row.get("catalog_slug") or "")
    if entry is None:
        return None
    return {
        "name": entry["name"],
        "description": entry["description"],
        "instructions": entry["instructions"],
    }


def build_skills_prompt(rows: List[Dict[str, Any]]) -> str:
    """Render the system-prompt rider for a list of enabled skill rows.

    Returns "" when nothing is enabled, so callers can unconditionally append
    the result without growing the prompt for users who use no skills.
    """
    resolved = [r for r in (resolve_enabled_skill(row) for row in rows) if r]
    if not resolved:
        return ""
    parts = [
        "\n\n",
        "── ACTIVE SKILLS ──\n",
        "The user has enabled the following skills. Apply the matching skill "
        "whenever the situation in its description arises. If several apply, "
        "combine them sensibly.\n",
    ]
    for skill in resolved:
        parts.append("\n")
        parts.append(skill["instructions"].rstrip())
        parts.append("\n")
    return "".join(parts)
