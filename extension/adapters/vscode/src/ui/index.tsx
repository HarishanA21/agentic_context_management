import { createRoot } from 'react-dom/client';
import { App, ContextWindow } from './App';
import './styles.css';

// `window.acmMount` is injected by the host (webview.ts -> renderHtml). The
// standalone "Context Window" editor tab mounts just that view full-screen;
// every other placement (sidebar view + settings panel) renders the full app.
const mount = (window as any).acmMount as string | undefined;

const el = document.getElementById('root');
if (el) {
  createRoot(el).render(mount === 'context-window' ? <ContextWindow standalone /> : <App />);
}
