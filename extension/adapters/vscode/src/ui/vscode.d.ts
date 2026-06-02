// Ambient types for the webview side (acquireVsCodeApi is injected by VSCode).
declare function acquireVsCodeApi(): {
  postMessage(msg: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
};

interface Window {
  acmMount?: string;
}
