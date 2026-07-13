# ACM Gateway

ACM keeps your AI assistant's context window small and useful. This package is the **gateway**, a small local service that does the context work. It trims, summarises, and manages what goes to the model on every call.

The gateway works with VSCode, Cursor, Antigravity, Claude Code, and Windsurf. Your IDE talks to the gateway as if it were the AI. The gateway applies the techniques, then passes the request on to the real provider.

## Install

Use `uv` to install the gateway.

```bash
uv tool install acm-context-management
```

## Run

Start the gateway. It serves on `http://127.0.0.1:8807`.

```bash
acm-gateway
```

To use your own API key, set it first.

```bash
export ACM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
export ACM_UPSTREAM_API_KEY=your_key_here
acm-gateway
```

## Point your IDE at the gateway

Tell your IDE the AI lives at `http://127.0.0.1:8807/v1`.

1. **Cursor.** Set a custom OpenAI base URL.
2. **Claude Code.** Set `ANTHROPIC_BASE_URL`.

## Profiles

A profile decides which techniques are on. Pick one of `minimal`, `long_chat`, `power_research`, `cheap_long`, or `visual_recall`. You can also copy the example config and edit it, or set `ACM_CONFIG` to your own file path.

## What the gateway does

1. **Trim.** Cut old and large tool outputs.
2. **Summarise.** Compress old turns into a short note.
3. **Sliding window.** Keep the newest turns and the system prompt.
4. **Image recall.** Cache or evict old images.
5. **Visual method.** Turn big tool outputs into an image the model reads.
6. **Manual removal.** Drop any message by hand. Choices are saved.
7. **Memory.** Remember and recall notes across sessions.
8. **Multi provider routing.** Route to OpenAI, OpenRouter, Google, Azure, or Anthropic.

## License

MIT
