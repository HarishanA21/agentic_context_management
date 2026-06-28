// Language Model Tools — the cross-agent reach inside VSCode. These register
// with vscode.lm.registerTool and become callable by Copilot agent mode (and
// any chat that references #acmRecall etc.). Each tool is a thin wrapper over
// the gateway control plane (see acmClient.ts).

import * as vscode from 'vscode';
import { AcmClient } from './acmClient';

function text(result: string): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(result)]);
}

export function registerTools(
  context: vscode.ExtensionContext,
  client: () => AcmClient,
): void {
  context.subscriptions.push(
    vscode.lm.registerTool('acm_remember', {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<{ text: string; scope?: string }>) {
        const { text: t, scope } = options.input;
        const res = await client().remember(t, scope ?? 'user');
        return text(`Saved to ${scope ?? 'user'} memory (now ${res.count} items).`);
      },
    }),
  );

  context.subscriptions.push(
    vscode.lm.registerTool('acm_recall', {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<{ query?: string; scope?: string }>) {
        const { query, scope } = options.input;
        const res = await client().recall(query ?? '', scope ?? 'user');
        return text(res.items.length ? res.items.map((i) => `- ${i}`).join('\n') : '(no matching memories)');
      },
    }),
  );

  context.subscriptions.push(
    vscode.lm.registerTool('acm_compact', {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<{ text: string }>) {
        const res = await client().compact(options.input.text);
        return text(res.summary);
      },
    }),
  );

  context.subscriptions.push(
    vscode.lm.registerTool('acm_set_profile', {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<{ name: string }>) {
        await client().setProfile(options.input.name);
        return text(`Active context-management profile set to '${options.input.name}'.`);
      },
    }),
  );
}
