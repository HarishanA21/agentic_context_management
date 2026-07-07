# Context Management (ACM)

Keep your AI assistant's context window small and useful. ACM trims, summarises, and remembers context for you, and it shows you exactly what goes to the model on every call.

ACM works with **GitHub Copilot agent mode** and **Claude Code**. It runs a small local helper called the **gateway** on `http://127.0.0.1:8807`. The gateway does the context work. The extension gives you tools, a settings panel, and a live context window view.

## Works with API keys and with subscriptions

ACM works two ways:

1. **With your own API key.** You bring a key and ACM manages the context for every request.
2. **With a subscription plan.** You need no extra key. ACM works with Claude Code in VSCode, Cursor, Antigravity, and Windsurf.

## See and control the Claude Code context window

ACM puts you in charge of the Claude Code context window on Pro, Max, and API:

1. **See everything sent on every call.** The Context Window view shows a live breakdown of what is in the window and how many tokens it costs.
2. **Apply techniques for each task.** Trim, summarise, drop messages, recall memory, and switch profiles to shape the window.
3. **Same control on Pro and Max as on API.** Claude Code runs through the local gateway, so the same controls apply.

## Features

1. **Language model tools.** `acm_remember`, `acm_recall`, `acm_compact`, and `acm_set_profile`. Call them from Copilot agent mode or in chat.
2. **Memory that lasts.** Remember decisions, file paths, and open tasks across sessions.
3. **Context window view.** A live view of the current chat's token usage, with the option to drop messages by hand.
4. **`@acm` chat participant.** Type `@acm status` or `@acm recall <query>` in the chat panel.
5. **Settings panel.** A UI for presets, technique settings, providers, and memory.
6. **Claude Code routing.** Point Claude Code at the gateway. Your own login token is forwarded, so a subscription needs no extra key.
7. **Profiles.** Pick `minimal`, `long_chat`, `power_research`, `cheap_long`, or `visual_recall`.

## Requirements

Nothing to install by hand. The extension talks to the gateway, a small local service. On first launch the extension installs the gateway for you and keeps it running. You will see a one time notice that says the gateway is setting up. If a gateway is already running, the extension uses it instead of starting a new one.

The only need on that first run is network access, so the gateway can download. After that it runs fully on your machine.

Status and profile checks need no key. To make model calls with your own API key, set the key first. A subscription plan needs no key.

```bash
export ACM_UPSTREAM_API_KEY=your_key_here
```

Want to run the gateway yourself? Set `acm.manageGateway` to `false` and start your own, or set `acm.gatewayCommand` to your launch command.

## Getting started

1. Install the extension from the Marketplace.
2. Open the **ACM** view from the activity bar. The gateway sets itself up on first launch.
3. In Copilot agent mode the ACM tools are ready. Reference them in chat with `#acmRemember`, `#acmRecall`, and so on.
4. Optional. Run **ACM: Monitor Claude Code** to route Claude Code through the gateway.

## Commands

**ACM: Open Context Management Settings.** Open the settings panel.

**ACM: Show Context Window.** Open the live context window view.

**ACM: Show Gateway Status.** Show gateway health.

**ACM: Recall Memory.** Recall stored facts.

**ACM: Monitor Claude Code.** Route Claude Code through the gateway.

**ACM: Stop Monitoring Claude Code.** Restore Claude Code routing.

**ACM: Restart Gateway.** Restart the managed gateway.

## Settings

**acm.gatewayUrl.** Default `http://127.0.0.1:8807`. Base URL of the local gateway.

**acm.memoryScope.** Default `user`. Default scope for memory tools. Use `user` or `thread`.

**acm.manageGateway.** Default `true`. Start and keep the gateway running for you.

**acm.gatewayCommand.** Default `acm-gateway`. Command used to launch the gateway.

**acm.routeClaudeCode.** Default `true`. Point Claude Code at the gateway.

**acm.routeScope.** Default `user`. Where to set the Claude Code routing. `user` writes to `~/.claude/settings.json`. `project` writes to your workspace settings.

## Other IDEs

The extension is also on **Open VSX**, so it installs in **Cursor**, **Antigravity**, and **Windsurf**. For any IDE that allows a custom endpoint, point it at `http://127.0.0.1:8807/v1`.

## Links

Repository: https://github.com/HarishanA21/agentic_context_management

Issues: https://github.com/HarishanA21/agentic_context_management/issues

## License

MIT
