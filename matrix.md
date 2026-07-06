## Master Matrix

| Feature | Scenario | Priority | Expected Result | Status |
|---|---|---|---|---|
| Chat | Create new standalone chat | High | New chat appears in sidebar under CHATS, opens empty thread | Done |
| Chat | Send message, get reply | High | User message renders instantly; assistant reply streams in | Done |
| Chat | Cancel an in-flight response | Med | Stream stops cleanly, partial output stays, no crash | Done |
| Chat | Resume/continue after cancel | Med | Can send another message immediately after cancel | Done |
| Chat | Chat has sandbox | Med | Sandbox works everywhere | Done |
| Chat | Rename / delete chat | Low | Sidebar updates, thread disappears from CHATS on delete | Done |
| Project | Create new project (blank) | High | New project in PROJECTS, `architecture.md` and `report.md` | Done |
| Project | Create project with file upload | High | Uploaded files visible in project file list | Done |
| Project | Create project linked to GitHub repo | Med | Repo metadata attached; files pulled in if `github_mode` supports it | Done |
| Project | Chat inside a project | High | Messages scoped to that project; context persists across its threads | Done |
| Project | Multiple threads inside one project share context | Med | A fact stated in thread A is recallable in thread B of same project | Done |
| MCP | View Catalog tab | High | Built-in MCP servers list from `mcp_catalog.json` | Done |
| MCP | Quick-enable a catalog MCP server | High | Server moves to "Yours" tab, shows enabled | Done |
| MCP | Add a Custom MCP server | High | Form accepts name/transport/command-args or endpoint+auth; saves | Done |
| MCP | Test connection on a server | High | "Test" button returns success + discovered tool list, or clear error | Done |
| MCP | Toggle enable/disable | Med | Tools appear/disappear from agent's toolset accordingly | Done |
| MCP | Delete a custom MCP server | Med | Removed from "Yours", no orphaned reference | Done |
| MCP | Add server with bad auth (negative test) | Med | Test connection fails with readable error, doesn't crash UI | Done |
| Sandbox | Ask agent to run a shell command inside a Project | High | `run_shell` executes, output returned (may require Docker) | |
| Sandbox | Command needing confirmation (e.g. `rm -rf`, `git push`, `sudo`) | High | Agent pauses for explicit human approval before executing | |
| Sandbox | Output larger than 16KB | Low | Output truncated/capped gracefully, not a crash | |
| Sandbox | Docker not running (negative test) | Med | Clear error surfaced, not a silent hang | |
| Context Profiles | View built-in profiles (e.g. "minimal") | High | Listed as read-only, can't edit/delete | |
| Context Profiles | Create a custom profile | High | Saved, selectable when starting/continuing a chat | |
| Context Profiles | Edit a custom profile | Med | Changes persist and take effect on next chat turn | |
| Context Profiles | Delete a custom profile | Med | Removed from list; chats previously using it fall back correctly | |
| Context Profiles | Hit the 25-profile cap (negative test) | Low | Creation blocked with clear message at 26th profile | |
| Context Profiles | Profile priority resolution | Med | Per-turn override > session default > user default > "minimal" — verify by setting at each level | |
| Strategy Demo | Run one prompt across strategies | High | Parallel columns for `tool_calling` vs `ts_code_mode`, both complete | |
| Strategy Demo | Compare streaming output | Med | Tokens stream live per column (SSE), up to 4 columns concurrently | |
| Strategy Demo | Confirm no side effects | Med | Demo run does NOT create a real chat/session in sidebar, no `write_project_file`/`run_shell` available | |
| Skills | Enable a catalog skill | High | Toggled on; its instructions affect subsequent chat behavior | |
| Skills | Disable a catalog skill | Med | Effect reverts | |
| Skills | Create a custom skill | High | Appears in list as `custom:<uuid>`, editable | |
| Skills | Edit / delete custom skill | Med | Updates persist / removal is clean | |
| Skills | Trigger skill via `/` slash menu in composer | High | Slash menu lists enabled skills, selecting one activates it for that message | |
| Plugins | Enable a plugin (e.g. `web-fetch`) | High | Tool becomes available to agent; asking it to fetch a URL works | |
| Plugins | Disable a plugin | Med | Tool no longer invocable, agent declines/doesn't call it | |
| Plugins | Confirm no create/delete UI | Low | Only enable/disable controls exist — this is expected, not a bug | |

---

