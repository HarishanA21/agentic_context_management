import { createRoot } from 'react-dom/client';
import { App, ContextWindow, ChatDetail } from './App';
import { chatConv } from './bridge';
import './styles.css';

// `window.acmMount` is injected by the host (webview.ts -> renderHtml):
//   'context-window' — the standalone Context Window editor tab
//   'chat'           — one chat's two-column detail (context window | settings)
//   anything else    — the full app (sidebar view + settings panel)
const mount = (window as any).acmMount as string | undefined;

const el = document.getElementById('root');
if (el) {
  const root = createRoot(el);
  root.render(
    mount === 'context-window' ? <ContextWindow standalone />
    : mount === 'chat' ? <ChatDetail conv={chatConv} />
    : <App />,
  );
}
