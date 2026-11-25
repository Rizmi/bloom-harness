import { BloomApiClient, BloomApiError } from './api-client.js';

const INLINE_IMAGE_MIMES = new Set([
  'image/png',
  'image/jpeg',
  'image/jpg',
  'image/gif',
  'image/webp',
]);
const MAX_ATTACH_BYTES = 25 * 1024 * 1024;

let markedLoadPromise = null;
function ensureMarkedLoaded() {
  if (typeof window !== 'undefined' && window.marked) {
    return Promise.resolve(window.marked);
  }
  if (!markedLoadPromise) {
    markedLoadPromise = new Promise((resolve) => {
      const src = new URL('./lib/marked.min.js', import.meta.url).href;
      const script = document.createElement('script');
      script.src = src;
      script.async = true;
      script.onload = () => resolve(window.marked || null);
      script.onerror = () => resolve(null);
      document.head.appendChild(script);
    });
  }
  return markedLoadPromise;
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function sanitizeHtmlOutput(html) {
  // Strip raw script/iframe/object tags and inline event handlers while preserving GFM markup.
  return html
    .replace(/<(script|iframe|object|embed|style)[^>]*>[\s\S]*?<\/\1>/gi, '')
    .replace(/<(script|iframe|object|embed)[^>]*\/?>/gi, '')
    .replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/(href|src)\s*=\s*(['"])\s*javascript:[^'"]*\2/gi, '$1="#"');
}

function cssEscape(value) {
  const str = value === null || value === undefined ? '' : String(value);
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(str);
  }
  return str.replace(/["\\\]]/g, '\\$&');
}

function formatTokenCount(num) {
  if (!num || num <= 0) return '0';
  if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(num);
}

function formatAttachmentSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isInlineImageMime(mime) {
  return INLINE_IMAGE_MIMES.has(String(mime || '').toLowerCase());
}

function sanitizeSrc(url) {
  const s = String(url || '').trim();
  if (!s) return '';
  if (/^(blob:|data:image\/(png|jpeg|jpg|gif|webp);|https?:\/\/|\/)/i.test(s)) {
    return escapeHtml(s);
  }
  return '';
}

function fallbackMarkdown(text) {
  if (!text) return '';
  let out = escapeHtml(text);
  out = out.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  out = out.replace(/^### (.*$)/gim, '<h3>$1</h3>');
  out = out.replace(/^## (.*$)/gim, '<h2>$1</h2>');
  out = out.replace(/^# (.*$)/gim, '<h1>$1</h1>');

  const lines = out.split('\n');
  const result = [];
  let tableLines = [];

  const flushTable = () => {
    if (tableLines.length >= 2) {
      const parseRow = (line) =>
        line.split('|').map((c) => c.trim()).filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);
      const headers = parseRow(tableLines[0]);
      const rows = tableLines.slice(2).map(parseRow);
      let html = '<div class="table-wrapper"><table><thead><tr>';
      for (const h of headers) html += `<th>${h}</th>`;
      html += '</tr></thead><tbody>';
      for (const r of rows) {
        html += '<tr>';
        for (let i = 0; i < headers.length; i++) html += `<td>${r[i] || ''}</td>`;
        html += '</tr>';
      }
      html += '</tbody></table></div>';
      result.push(html);
    } else {
      result.push(...tableLines);
    }
    tableLines = [];
  };

  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith('|') && t.endsWith('|')) {
      tableLines.push(t);
    } else {
      if (tableLines.length) flushTable();
      result.push(line);
    }
  }
  if (tableLines.length) flushTable();
  return result.join('<br>');
}

function formatMarkdown(text) {
  if (!text) return '';
  const m = typeof window !== 'undefined' ? window.marked : null;
  if (m && (typeof m.parse === 'function' || typeof m === 'function')) {
    try {
      const parseFn = typeof m.parse === 'function' ? m.parse.bind(m) : m;
      let html = parseFn(text, { breaks: true, gfm: true });
      html = html
        .replace(/<table>/g, '<div class="table-wrapper"><table>')
        .replace(/<\/table>/g, '</table></div>')
        .replace(/<a\s+(?!target=)/gi, '<a target="_blank" rel="noopener noreferrer" ');
      return sanitizeHtmlOutput(html);
    } catch {
      // Fall through to plain formatter if marked throws
    }
  }
  return fallbackMarkdown(text);
}

// Inline SVG icons (zero CDN dependency, exact crisp geometry)
const ICONS = {
  terminal: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2.5 4.5 6.5 8l-4 3.5"/><path d="M8 11.5h5.5"/></svg>',
  chip: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="4" y="4" width="8" height="8"/><path d="M6 1.5v2.5M10 1.5v2.5M6 12v2.5M10 12v2.5M1.5 6H4M1.5 10H4M12 6h2.5M12 10h2.5"/></svg>',
  refresh: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13.5 8a5.5 5.5 0 1 1-1.4-3.7"/><path d="M13.5 2.5v3.2h-3.2"/></svg>',
  bolt: '<svg class="ico" viewBox="0 0 16 16" fill="currentColor"><path d="M9 1.5 3.5 9H8l-1 5.5L12.5 7H8l1-5.5Z"/></svg>',
  plus: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 3v10M3 8h10"/></svg>',
  wrench: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M10.8 2.2a3.2 3.2 0 0 0-3.7 4.4L2.4 11.3a1.3 1.3 0 0 0 1.8 1.8l4.7-4.7a3.2 3.2 0 0 0 4.4-3.7l-1.9 1.9-1.5-.5-.5-1.5 1.4-2.4Z"/></svg>',
  brain: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M6 3a2 2 0 0 0-3.5 1.3A2.3 2.3 0 0 0 2 8a2.3 2.3 0 0 0 .8 3.6A2 2 0 0 0 6.5 13V3ZM10 3a2 2 0 0 1 3.5 1.3A2.3 2.3 0 0 1 14 8a2.3 2.3 0 0 1-.8 3.6A2 2 0 0 1 9.5 13V3Z"/></svg>',
  chevronRight: '<svg class="ico ico-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>',
  check: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M3 8.5 6.5 12 13 4.5"/></svg>',
  spinner: '<svg class="ico ico-spin" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M8 2a6 6 0 1 1-4.9 2.5" stroke-linecap="square"/></svg>',
  warn: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2 14.2 13H1.8L8 2Z"/><path d="M8 6.3v3.2M8 11.2v.6"/></svg>',
  ban: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5.8"/><path d="M3.9 3.9 12.1 12.1"/></svg>',
  shield: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.8 13.5 4v4c0 3.3-2.3 5.4-5.5 6.3C4.8 13.4 2.5 11.3 2.5 8V4L8 1.8Z"/></svg>',
  clip: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11.5 5.2 6.3 10.4a1.8 1.8 0 0 1-2.5-2.5l5.8-5.8a2.8 2.8 0 1 1 4 4L7.4 12.3a3.8 3.8 0 0 1-5.4-5.4l5.1-5.1"/></svg>',
  send: '<svg class="ico" viewBox="0 0 16 16" fill="currentColor"><path d="M14.5 1.8 1.5 7.2l4.7 1.9 1.9 4.7 6.4-12ZM6.9 9.5l4.6-4.6-3.1 6-1.5-1.4Z"/></svg>',
  stop: '<svg class="ico" viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="3.5" width="9" height="9"/></svg>',
  file: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3.5 2h6l3 3v9h-9V2Z"/><path d="M9.5 2v3h3"/></svg>',
  arrowIn: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 8h8M7.5 5.5 10 8l-2.5 2.5M12 3v10"/></svg>',
  arrowOut: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 8h8M11.5 5.5 14 8l-2.5 2.5M4 3v10"/></svg>',
  expand: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 2v12M11 2v12M2 8h3M11 8h3M3.8 6.5 2 8l1.8 1.5M12.2 6.5 14 8l-1.8 1.5"/></svg>',
  key: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5.2" cy="8" r="2.8"/><path d="M8 8h6M12 8v2.5M14 8v2"/></svg>',
};

const STYLES = `
:host {
  --bloom-bg: #09090b;
  --bloom-surface: #111114;
  --bloom-surface-2: #18181b;
  --bloom-border: #27272a;
  --bloom-border-hover: #3f3f46;
  --bloom-text: #f4f4f5;
  --bloom-text-muted: #a1a1aa;
  --bloom-text-dim: #71717a;
  --bloom-accent: #34d399;
  --bloom-cyan: #38bdf8;
  --bloom-amber: #fbbf24;
  --bloom-rose: #fb7185;
  --bloom-font-sans: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --bloom-font-mono: 'Geist Mono', 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--bloom-bg);
  color: var(--bloom-text);
  font-family: var(--bloom-font-sans);
  font-size: 12px;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}

*, *::before, *::after {
  box-sizing: border-box;
  border-radius: 0 !important;
}

button, input, select, textarea {
  font-family: inherit;
}

code, pre, .font-mono {
  font-family: var(--bloom-font-mono) !important;
}

.ico {
  width: 12px;
  height: 12px;
  display: inline-block;
  flex-shrink: 0;
  vertical-align: -0.15em;
}
.ico-spin {
  animation: bloom-spin 0.9s linear infinite;
}
@keyframes bloom-spin {
  to { transform: rotate(360deg); }
}
@keyframes bloom-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.pulse {
  animation: bloom-pulse 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite;
}

/* Custom scrollbars */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #09090b; }
::-webkit-scrollbar-thumb { background: #27272a; }
::-webkit-scrollbar-thumb:hover { background: #3f3f46; }
.no-scrollbar::-webkit-scrollbar { display: none; width: 0; height: 0; }
.no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }

.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bloom-bg);
}

/* Optional Top Controls Bar (mirrors host header controls) */
.topbar {
  height: 48px;
  padding: 0 14px;
  border-bottom: 1px solid var(--bloom-border);
  background: var(--bloom-bg);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-shrink: 0;
}
.topbar.hidden { display: none; }

.topbar-left, .topbar-center, .topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.brand-box {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-mark {
  width: 28px;
  height: 28px;
  background: var(--bloom-surface-2);
  border: 1px solid var(--bloom-border);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--bloom-accent);
}
.brand-name {
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #f4f4f5;
  white-space: nowrap;
}
.brand-pill {
  font-family: var(--bloom-font-mono);
  font-size: 11px;
  padding: 2px 8px;
  background: var(--bloom-surface-2);
  border: 1px solid var(--bloom-border);
  color: #d4d4d8;
  white-space: nowrap;
}

.ctrl-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 10px;
  height: 28px;
  background: var(--bloom-surface-2);
  border: 1px solid var(--bloom-border);
  color: #d4d4d8;
  font-size: 11px;
  white-space: nowrap;
}
.status-dot {
  width: 6px;
  height: 6px;
  background: var(--bloom-accent);
  flex-shrink: 0;
}
.status-dot.offline { background: var(--bloom-rose); }
.status-dot.idle { background: #52525b; }
.status-dot.busy { background: var(--bloom-accent); animation: bloom-pulse 1.4s infinite; }

.model-select {
  background: transparent;
  color: #e4e4e7;
  border: none;
  outline: none;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  max-width: 240px;
}
.model-select option, .model-select optgroup {
  background: #111114;
  color: #e4e4e7;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 11px;
  background: var(--bloom-surface-2);
  border: 1px solid var(--bloom-border);
  color: #d4d4d8;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
  flex-shrink: 0;
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s, border-color 0.15s;
}
.btn:hover:not(:disabled) {
  background: #27272a;
  color: #ffffff;
  border-color: #3f3f46;
}
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-icon { width: 28px; padding: 0; justify-content: center; }

.session-id-badge {
  font-family: var(--bloom-font-mono);
  font-size: 11px;
  color: #d4d4d8;
  background: var(--bloom-surface-2);
  border: 1px solid var(--bloom-border);
  padding: 4px 8px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  max-width: 210px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Auth bar when BLOOM_API_TOKEN is required standalone */
.auth-bar {
  display: none;
  padding: 8px 12px;
  background: rgba(245, 158, 11, 0.08);
  border-bottom: 1px solid rgba(245, 158, 11, 0.35);
  align-items: center;
  gap: 8px;
  font-family: var(--bloom-font-mono);
  font-size: 11px;
  color: #fde68a;
  flex-shrink: 0;
}
.auth-bar.visible { display: flex; }
.auth-input {
  flex: 1;
  max-width: 280px;
  height: 26px;
  padding: 0 8px;
  background: #09090b;
  border: 1px solid #3f3f46;
  color: #f4f4f5;
  font-size: 11px;
  outline: none;
}
.auth-input:focus { border-color: #a1a1aa; }

/* Chat Header */
.chat-header {
  padding: 11px 12px;
  border-bottom: 1px solid var(--bloom-border);
  background: var(--bloom-bg);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-shrink: 0;
}
.chat-title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.title-square {
  width: 6px;
  height: 6px;
  background: #a1a1aa;
  flex-shrink: 0;
}
.chat-title {
  font-family: var(--bloom-font-mono);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.025em;
  color: #e4e4e7;
  truncate: ellipsis;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.token-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  font-size: 10.5px;
  font-family: var(--bloom-font-mono);
  background: #18181b;
  border: 1px solid #27272a;
  color: #d4d4d8;
  user-select: none;
  cursor: help;
}
.token-badge.tone-ok { border-color: rgba(6, 78, 59, 0.55); background: rgba(2, 44, 34, 0.25); }
.token-badge.tone-warn { border-color: rgba(120, 53, 15, 0.65); background: rgba(69, 26, 3, 0.25); }
.token-badge.tone-danger { border-color: rgba(136, 19, 55, 0.7); background: rgba(76, 5, 25, 0.35); }
.token-ico { color: #71717a; }
.token-badge.tone-ok .token-ico { color: #34d399; }
.token-badge.tone-warn .token-ico { color: #fbbf24; }
.token-badge.tone-danger .token-ico { color: #fb7185; }

/* Session Tabs Bar (28px ultra-compact) */
.tabs-bar {
  height: 28px;
  padding: 0 8px;
  background: var(--bloom-bg);
  border-bottom: 1px solid var(--bloom-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  flex-shrink: 0;
  user-select: none;
  overflow: hidden;
}
.tabs-list {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  padding: 2px 0;
  flex: 1;
  min-width: 0;
}
.tab-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  font-size: 11px;
  font-family: var(--bloom-font-mono);
  cursor: pointer;
  border: 1px solid transparent;
  background: transparent;
  color: #a1a1aa;
  flex-shrink: 0;
  transition: background-color 0.12s, color 0.12s, border-color 0.12s;
}
.tab-item:hover {
  color: #e4e4e7;
  background: rgba(24, 24, 27, 0.6);
}
.tab-item.active {
  background: #18181b;
  color: #f4f4f5;
  border-color: #3f3f46;
}
.tab-dot {
  width: 6px;
  height: 6px;
  background: #52525b;
  flex-shrink: 0;
}
.tab-dot.busy {
  background: var(--bloom-accent);
  animation: bloom-pulse 1.4s infinite;
}
.tab-label {
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tab-close {
  background: transparent;
  border: none;
  color: #71717a;
  font-size: 13px;
  line-height: 1;
  padding: 0 2px;
  cursor: pointer;
  margin-left: 2px;
}
.tab-close:hover { color: var(--bloom-rose); }
.btn-new-tab {
  height: 20px;
  padding: 0 7px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #a1a1aa;
  background: rgba(24, 24, 27, 0.85);
  border: 1px solid var(--bloom-border);
  font-size: 10px;
  font-family: var(--bloom-font-mono);
  cursor: pointer;
  flex-shrink: 0;
}
.btn-new-tab:hover:not(:disabled) {
  color: #f4f4f5;
  background: #27272a;
}
.btn-new-tab:disabled { opacity: 0.5; cursor: not-allowed; }

/* Messages Area */
.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.msg-card {
  padding: 12px;
  border: 1px solid var(--bloom-border);
  font-size: 12px;
  line-height: 1.55;
  background: var(--bloom-bg);
  flex-shrink: 0;
}
.msg-user {
  background: #18181b;
  border-color: rgba(63, 63, 70, 0.8);
  color: #f4f4f5;
  margin-left: 24px;
}
.msg-assistant {
  background: #09090b;
  border-color: #27272a;
  color: #e4e4e7;
  margin-right: 24px;
}
.msg-system {
  background: #09090b;
  border-color: #1c1c20;
  color: #a1a1aa;
  font-size: 11px;
  font-family: var(--bloom-font-mono);
}
.welcome-card {
  padding: 12px;
  background: rgba(24, 24, 27, 0.6);
  border: 1px solid var(--bloom-border);
  font-size: 12px;
  color: #d4d4d8;
  line-height: 1.55;
  flex-shrink: 0;
}
.welcome-title {
  font-family: var(--bloom-font-mono);
  font-weight: 600;
  color: #f4f4f5;
  margin: 0 0 6px 0;
  display: flex;
  align-items: center;
  gap: 6px;
}
.welcome-session-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  padding: 2px 8px;
  background: #09090b;
  border: 1px solid var(--bloom-border);
  font-family: var(--bloom-font-mono);
  font-size: 10px;
  color: #d4d4d8;
}

.role-header {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #a1a1aa;
  font-family: var(--bloom-font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}
.role-header .term-ico { color: var(--bloom-accent); }
.role-header.sys { color: #71717a; margin-bottom: 3px; }

/* Reasoning & Tool Execution Collapsibles */
details.section-box {
  margin-bottom: 10px;
  border: 1px solid var(--bloom-border);
  background: rgba(24, 24, 27, 0.4);
  font-size: 11px;
  font-family: var(--bloom-font-mono);
}
details.section-box > summary {
  padding: 6px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
  color: #a1a1aa;
  list-style: none;
}
details.section-box > summary::-webkit-details-marker { display: none; }
details.section-box > summary:hover { color: #e4e4e7; }
details.section-box[open] > summary .ico-chevron { transform: rotate(90deg); }
.ico-chevron { transition: transform 0.18s ease; color: #71717a; width: 10px; height: 10px; }

.sum-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
}
.sum-title { font-weight: 600; color: #d4d4d8; }
.sum-count-running { color: var(--bloom-amber); font-size: 9.5px; }
.sum-count-done { color: #71717a; font-size: 9.5px; }
.reasoning-badge { font-size: 9.5px; color: #71717a; font-family: var(--bloom-font-sans); font-style: italic; }

.reasoning-body {
  padding: 8px 10px;
  border-top: 1px solid var(--bloom-border);
  color: #a1a1aa;
  white-space: pre-wrap;
  line-height: 1.5;
  max-height: 224px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.55);
  user-select: text;
}

.tools-scroll-list {
  padding: 8px;
  border-top: 1px solid var(--bloom-border);
  max-height: 288px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.4);
  display: block;
}

details.tool-item {
  display: block;
  background: #09090b;
  border: 1px solid var(--bloom-border);
  font-size: 10.5px;
  font-family: var(--bloom-font-mono);
  overflow: hidden;
  flex-shrink: 0;
}
details.tool-item + details.tool-item {
  margin-top: 6px;
}
details.tool-item:hover { border-color: #3f3f46; }
details.tool-item > summary {
  padding: 6px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
  user-select: none;
  color: #a1a1aa;
  list-style: none;
}
details.tool-item > summary::-webkit-details-marker { display: none; }
details.tool-item > summary:hover { color: #e4e4e7; }
details.tool-item[open] > summary .ico-chevron { transform: rotate(90deg); }

.tool-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.tool-name { color: #e4e4e7; font-weight: 700; flex-shrink: 0; }
.tool-preview {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
  color: #71717a;
}

.badge {
  padding: 1px 6px;
  font-family: var(--bloom-font-mono);
  font-size: 9px;
  font-weight: 500;
  border: 1px solid #27272a;
  background: #18181b;
  color: #a1a1aa;
  flex-shrink: 0;
}
.badge-done {
  background: rgba(16, 185, 129, 0.1);
  color: #34d399;
  border-color: rgba(16, 185, 129, 0.3);
}
.badge-running {
  background: rgba(245, 158, 11, 0.1);
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.3);
}
.badge-failed {
  background: rgba(244, 63, 94, 0.1);
  color: #fb7185;
  border-color: rgba(244, 63, 94, 0.3);
}
.ico-done { color: #34d399; }
.ico-running { color: #fbbf24; }
.ico-failed, .ico-stopped { color: #fb7185; }

.tool-body {
  padding: 8px 10px;
  border-top: 1px solid var(--bloom-border);
  background: #09090b;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.io-label {
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #71717a;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 4px;
}
.io-pre {
  background: #000000;
  border: 1px solid #1e1e22;
  padding: 8px;
  color: #e4e4e7;
  font-family: var(--bloom-font-mono);
  font-size: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
  max-height: 210px;
  overflow: auto;
  margin: 4px 0 0 0;
  user-select: text;
}

/* Permission Approval Card */
.perm-card {
  border: 1px solid rgba(244, 63, 94, 0.45);
  background: rgba(244, 63, 94, 0.06);
  padding: 10px 12px;
  font-size: 11px;
  margin: 6px 0 10px 0;
}
.perm-title {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #fda4af;
  font-weight: 600;
}
.perm-action { color: #e4e4e7; margin-top: 4px; font-family: var(--bloom-font-mono); }
.perm-resources {
  margin-top: 4px;
  font-size: 10px;
  color: #a1a1aa;
  font-family: var(--bloom-font-mono);
  word-break: break-all;
}
.perm-buttons {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.btn-allow {
  padding: 4px 10px;
  background: #059669;
  border: 1px solid #10b981;
  color: #ffffff;
  font-weight: 600;
  font-size: 11px;
  cursor: pointer;
}
.btn-allow:hover:not(:disabled) { background: #10b981; }
.btn-reject {
  padding: 4px 10px;
  background: #be123c;
  border: 1px solid #f43f5e;
  color: #ffffff;
  font-weight: 600;
  font-size: 11px;
  cursor: pointer;
}
.btn-reject:hover:not(:disabled) { background: #e11d48; }
.perm-status { font-size: 10px; font-family: var(--bloom-font-mono); color: #71717a; }

/* Prose Markdown Styling (Exact match to host UI) */
.prose-chat {
  word-break: break-word;
  font-size: 12px;
  line-height: 1.55;
  color: #e4e4e7;
  user-select: text;
}
.prose-chat p { margin: 0 0 0.45rem 0; }
.prose-chat p:last-child { margin-bottom: 0; }
.prose-chat h1, .prose-chat h2, .prose-chat h3, .prose-chat h4 {
  font-weight: 700;
  color: #fafafa;
  margin-top: 0.85rem;
  margin-bottom: 0.35rem;
  line-height: 1.3;
}
.prose-chat h1 { font-size: 1.05rem; border-bottom: 1px solid #27272a; padding-bottom: 0.25rem; }
.prose-chat h2 { font-size: 0.95rem; border-bottom: 1px solid #27272a; padding-bottom: 0.2rem; color: #f4f4f5; }
.prose-chat h3 { font-size: 0.88rem; color: #e4e4e7; }
.prose-chat h4 { font-size: 0.8rem; color: #a1a1aa; }
.prose-chat ul {
  list-style-type: square;
  padding-left: 1.25rem;
  margin: 0.35rem 0 0.55rem 0;
}
.prose-chat ol {
  list-style-type: decimal;
  padding-left: 1.25rem;
  margin: 0.35rem 0 0.55rem 0;
}
.prose-chat li { margin-bottom: 0.25rem; }
.prose-chat li::marker { color: #71717a; }
.prose-chat blockquote {
  border-left: 2px solid #52525b;
  padding-left: 0.75rem;
  margin: 0.45rem 0;
  color: #a1a1aa;
  font-style: italic;
  background: #111114;
}
.prose-chat hr { border: 0; border-top: 1px solid #27272a; margin: 0.65rem 0; }
.prose-chat a { color: #38bdf8; text-decoration: underline; text-underline-offset: 2px; }
.prose-chat a:hover { color: #7dd3fc; }
.prose-chat strong { font-weight: 600; color: #ffffff; }
.prose-chat code:not(pre code) {
  background: #09090b;
  color: #38bdf8;
  padding: 0.1rem 0.35rem;
  font-size: 11px;
  border: 1px solid #27272a;
}
.prose-chat pre {
  background: #09090b;
  border: 1px solid #27272a;
  padding: 0.65rem 0.75rem;
  margin: 0.5rem 0;
  overflow-x: auto;
  font-size: 11px;
  line-height: 1.45;
  color: #e4e4e7;
}
.prose-chat pre code {
  background: transparent;
  padding: 0;
  border: none;
  color: inherit;
  font-size: inherit;
}

.prose-chat .table-wrapper {
  overflow-x: auto;
  margin: 0.75rem 0;
  border: 1px solid #27272a;
  background: #0c0c0e;
  max-width: 100%;
}
.prose-chat table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 11px;
  line-height: 1.45;
}
.prose-chat th {
  background: #141417;
  color: #a1a1aa;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 10px;
  padding: 7px 10px;
  border-bottom: 1px solid #27272a;
  border-right: 1px solid #222226;
  white-space: nowrap;
  font-family: var(--bloom-font-mono);
}
.prose-chat th:last-child { border-right: none; }
.prose-chat td {
  padding: 6px 10px;
  border-bottom: 1px solid #1c1c20;
  border-right: 1px solid #1c1c20;
  color: #e4e4e7;
  vertical-align: top;
}
.prose-chat td:last-child { border-right: none; }
.prose-chat tbody tr:last-child td { border-bottom: none; }
.prose-chat tbody tr:nth-child(even) td { background: rgba(24, 24, 27, 0.4); }
.prose-chat tbody tr:hover td { background: rgba(39, 39, 42, 0.5); }

.stream-raw { white-space: pre-wrap; line-height: 1.55; display: inline; }
.cursor-blink {
  display: inline-block;
  width: 6px;
  height: 14px;
  background: var(--bloom-accent);
  vertical-align: middle;
  margin-left: 2px;
  animation: bloom-pulse 1s infinite;
}
.working-line {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--bloom-accent);
  font-family: var(--bloom-font-mono);
  font-size: 11px;
  padding: 4px 0;
  animation: bloom-pulse 1.4s infinite;
}

/* Attachments */
.att-list-msg { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.att-img-link {
  display: block;
  position: relative;
  overflow: hidden;
  border: 1px solid #27272a;
}
.att-img-link:hover { border-color: #71717a; }
.att-img-link img { height: 80px; max-width: 160px; object-fit: cover; background: #000; display: block; }
.att-img-cap {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  background: rgba(0,0,0,0.82);
  font-size: 9px;
  font-family: var(--bloom-font-mono);
  padding: 2px 4px;
  color: #d4d4d8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.att-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  background: #18181b;
  border: 1px solid #27272a;
  font-size: 10px;
  font-family: var(--bloom-font-mono);
  color: #d4d4d8;
}

/* Input Area */
.input-area {
  padding: 12px;
  border-top: 1px solid var(--bloom-border);
  background: var(--bloom-bg);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}
.att-preview-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.att-preview-bar.hidden { display: none; }
.att-preview-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  background: #18181b;
  border: 1px solid #27272a;
  font-size: 10px;
  color: #d4d4d8;
}
.att-preview-card.err { border-color: rgba(244, 63, 94, 0.65); }
.att-thumb { width: 32px; height: 32px; object-fit: cover; border: 1px solid #27272a; }
.att-thumb-ico {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  background: #09090b; border: 1px solid #27272a; color: #a1a1aa;
}
.att-meta { display: flex; flex-direction: column; line-height: 1.2; }
.att-meta-name { font-family: var(--bloom-font-mono); max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.att-meta-sub { color: #71717a; }
.att-meta-err { color: var(--bloom-rose); }
.att-remove {
  background: transparent; border: none; color: #71717a; cursor: pointer; font-size: 13px; padding: 0 2px;
}
.att-remove:hover { color: var(--bloom-rose); }

.input-box {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  background: rgba(24, 24, 27, 0.6);
  border: 1px solid var(--bloom-border);
  padding: 8px;
  transition: border-color 0.15s, background-color 0.15s;
}
.input-box:focus-within { border-color: #a1a1aa; }
.input-box.drag-over { border-color: #d4d4d8; background: rgba(39, 39, 42, 0.55); }

.btn-attach {
  padding: 8px;
  background: transparent;
  border: 1px solid transparent;
  color: #a1a1aa;
  cursor: pointer;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.btn-attach:hover { color: #e4e4e7; background: #27272a; border-color: #3f3f46; }

.chat-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  color: #e4e4e7;
  font-size: 12px;
  line-height: 1.5;
  min-height: 36px;
  max-height: 160px;
}
.chat-input::placeholder { color: #71717a; }

.btn-send {
  padding: 8px;
  background: #f4f4f5;
  border: 1px solid #d4d4d8;
  color: #09090b;
  cursor: pointer;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.btn-send:hover { background: #ffffff; }
.btn-stop {
  padding: 8px;
  background: #e11d48;
  border: 1px solid #f43f5e;
  color: #ffffff;
  cursor: pointer;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.btn-stop:hover { background: #f43f5e; }
.hidden { display: none !important; }

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  font-family: var(--bloom-font-mono);
  color: #71717a;
  min-height: 16px;
}
.thinking-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #d4d4d8;
}

@media (max-width: 767px) {
  .topbar-brand-pill, .hint-desktop { display: none; }
  .model-select { max-width: 140px; }
  .msg-user { margin-left: 10px; }
  .msg-assistant { margin-right: 10px; }
}
`;

export class BloomChatElement extends HTMLElement {
  static get observedAttributes() {
    return ['api-base', 'token', 'agent', 'title', 'placeholder', 'hide-topbar'];
  }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });

    this._api = new BloomApiClient({
      apiBase: this.getAttribute('api-base') || '/api',
      token: this._resolveInitialToken(),
      agent: this.getAttribute('agent') || '',
    });

    this._info = null;
    this._connected = false;
    this._sessions = []; // [{ id, title, model, isBusy }]
    this._activeSessionId = null;
    this._tabCounter = 0;

    // Per-session ephemeral client state
    this._sessionState = new Map(); // sid -> { isThinking, abortController, draftInput, draftAttachments, bubble }
    this._sessionUsage = new Map(); // sid -> usage
    this._detailsOpen = new Map(); // openKey -> true
    this._closedIds = new Set();
    this._attachments = [];
    this._attachSeq = 0;
    this._msgCounter = 0;
    this._pollTimer = null;
    this._pollSessionId = null;
    this._bgPollTimer = null;
    this._initialized = false;

    this._renderSkeleton();
  }

  _storageKey(suffix) {
    const base = this.getAttribute('api-base') || '/api';
    return `bloom_${suffix}_${base}`;
  }

  _resolveInitialToken() {
    const attr = (this.getAttribute('token') || '').trim();
    if (attr) return attr;
    try {
      return (sessionStorage.getItem(this._storageKey('token')) || '').trim();
    } catch {
      return '';
    }
  }

  attributeChangedCallback(name, oldVal, newVal) {
    if (oldVal === newVal) return;
    if (name === 'api-base') {
      this._api.setApiBase(newVal || '/api');
      if (this._initialized) this.refresh();
    } else if (name === 'token') {
      this._api.setToken(newVal || '');
      if (this._initialized) this.refresh();
    } else if (name === 'agent') {
      this._api.agent = (newVal || '').trim();
      if (this._initialized) this.refresh();
    } else if (name === 'title') {
      this._updateTitleDom();
    } else if (name === 'placeholder') {
      const input = this.$('#chat-input');
      if (input) input.placeholder = newVal || 'Ask agent anything...';
    } else if (name === 'hide-topbar') {
      this._syncTopbarVisibility();
    }
  }

  connectedCallback() {
    ensureMarkedLoaded().then(() => {
      // Re-render current active view once marked finishes loading if already populated
      if (this._initialized && this._activeSessionId) {
        const entry = this._sessionState.get(this._activeSessionId);
        if (!entry?.isThinking) {
          this.loadChatHistory(this._activeSessionId);
        }
      }
    });
    this._bindEvents();
    this._syncTopbarVisibility();
    if (!this._initialized) {
      this._initialized = true;
      this.init();
    }
    this._bgPollTimer = setInterval(() => this._backgroundStatusTick(), 3500);
  }

  disconnectedCallback() {
    this._stopHistoryPoller();
    if (this._bgPollTimer) {
      clearInterval(this._bgPollTimer);
      this._bgPollTimer = null;
    }
  }

  $(sel) {
    return this.shadowRoot.querySelector(sel);
  }

  _renderSkeleton() {
    const style = document.createElement('style');
    style.textContent = STYLES;

    const shell = document.createElement('div');
    shell.className = 'shell';
    const placeholder = escapeHtml(this.getAttribute('placeholder') || 'Ask agent anything...');
    shell.innerHTML = `
      <div class="topbar" id="topbar">
        <div class="topbar-left">
          <div class="brand-box">
            <div class="brand-mark">${ICONS.terminal}</div>
            <span class="brand-name" id="brand-name">BLOOM</span>
            <span class="brand-pill topbar-brand-pill" id="brand-agent">assistant</span>
          </div>
        </div>
        <div class="topbar-center">
          <div class="ctrl-pill" id="conn-badge">
            <span class="status-dot" id="conn-dot"></span>
            <span id="conn-text">Connecting...</span>
          </div>
          <div class="ctrl-pill">
            ${ICONS.chip}
            <span class="font-mono" style="color:#a1a1aa">Model:</span>
            <select id="model-select" class="model-select">
              <option value="">Loading models...</option>
            </select>
          </div>
          <button id="btn-reset" class="btn" title="Reset active chat session & start fresh">
            ${ICONS.refresh}
            <span>Reset Chat</span>
          </button>
        </div>
        <div class="topbar-right">
          <button id="btn-refresh" class="btn btn-icon" title="Refresh status & history">${ICONS.refresh}</button>
          <span id="session-badge" class="session-id-badge" title="Active Agent Session">ID: --</span>
          <button id="btn-auth-toggle" class="btn btn-icon hidden" title="Set API token">${ICONS.key}</button>
        </div>
      </div>

      <div class="auth-bar" id="auth-bar">
        ${ICONS.key}
        <span>API token required:</span>
        <input type="password" id="auth-token-input" class="auth-input" placeholder="Paste BLOOM_API_TOKEN...">
        <button id="btn-auth-save" class="btn">Connect</button>
      </div>

      <div class="chat-header">
        <div class="chat-title-wrap">
          <span class="title-square"></span>
          <span class="chat-title" id="chat-title">Bloom Agent</span>
        </div>
        <div class="chat-header-right">
          <div id="token-badge" class="token-badge" title="Context Token Usage">
            <span class="token-ico">${ICONS.bolt}</span>
            <span id="token-text">Tokens: --</span>
          </div>
          <slot name="header-actions"></slot>
        </div>
      </div>

      <div class="tabs-bar">
        <div class="tabs-list no-scrollbar" id="tabs-list"></div>
        <button id="btn-new-tab" class="btn-new-tab" title="Start new concurrent session tab">
          ${ICONS.plus}<span>New</span>
        </button>
      </div>

      <div class="messages" id="messages"></div>

      <div class="input-area">
        <div class="att-preview-bar hidden" id="att-preview"></div>
        <div class="input-box" id="input-box">
          <input type="file" id="file-input" multiple class="hidden">
          <button type="button" id="btn-attach" class="btn-attach" title="Attach files or drag & drop / Ctrl+V paste">
            ${ICONS.clip}
          </button>
          <textarea id="chat-input" rows="2" class="chat-input" placeholder="${placeholder}"></textarea>
          <button type="button" id="btn-send" class="btn-send" title="Send message">${ICONS.send}</button>
          <button type="button" id="btn-stop" class="btn-stop hidden" title="Stop streaming">${ICONS.stop}</button>
        </div>
        <div class="input-footer">
          <span class="hint-desktop">Paste (Ctrl+V) / drag files</span>
          <span id="thinking-indicator" class="thinking-badge hidden">
            ${ICONS.spinner}<span>Processing...</span>
          </span>
        </div>
      </div>
    `;

    this.shadowRoot.innerHTML = '';
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(shell);
  }

  _syncTopbarVisibility() {
    const tb = this.$('#topbar');
    if (tb) {
      tb.classList.toggle('hidden', this.hasAttribute('hide-topbar'));
    }
  }

  _agentDisplayName() {
    return (this._info && this._info.active_agent) || this.getAttribute('agent') || 'assistant';
  }

  _updateTitleDom() {
    const custom = (this.getAttribute('title') || '').trim();
    const agent = this._agentDisplayName();
    const prettyAgent = agent.charAt(0).toUpperCase() + agent.slice(1);
    const titleEl = this.$('#chat-title');
    if (titleEl) {
      titleEl.textContent = custom || `${prettyAgent} Agent`;
    }
    const brandAgent = this.$('#brand-agent');
    if (brandAgent) {
      brandAgent.textContent = agent;
    }
  }

  _bindEvents() {
    const input = this.$('#chat-input');
    const fileInput = this.$('#file-input');
    const dropZone = this.$('#input-box');
    const messagesEl = this.$('#messages');

    // Remember user-toggled <details> across background re-renders
    messagesEl.addEventListener('click', (e) => {
      const summary = e.target && e.target.closest ? e.target.closest('summary') : null;
      if (!summary) return;
      const details = summary.parentElement;
      const key = details && details.getAttribute('data-open-key');
      if (!key) return;
      if (details.open) this._detailsOpen.delete(key);
      else this._detailsOpen.set(key, true);
    });

    this.$('#btn-attach').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => {
      Array.from(e.target.files || []).forEach((f) => this._addAttachmentFile(f));
      e.target.value = '';
    });

    this.$('#btn-send').addEventListener('click', () => this.sendPrompt());
    this.$('#btn-stop').addEventListener('click', () => this.abortActiveTurn());
    this.$('#btn-new-tab').addEventListener('click', () => this.createSessionTab());
    this.$('#btn-reset').addEventListener('click', () => this.resetActiveSession());
    this.$('#btn-refresh').addEventListener('click', () => this.refresh());

    this.$('#btn-auth-toggle').addEventListener('click', () => {
      this.$('#auth-bar').classList.toggle('visible');
      this.$('#auth-token-input').focus();
    });
    const saveAuth = () => {
      const tok = this.$('#auth-token-input').value.trim();
      this._api.setToken(tok);
      try {
        if (tok) sessionStorage.setItem(this._storageKey('token'), tok);
        else sessionStorage.removeItem(this._storageKey('token'));
      } catch {}
      this.$('#auth-bar').classList.remove('visible');
      this.init();
    };
    this.$('#btn-auth-save').addEventListener('click', saveAuth);
    this.$('#auth-token-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') saveAuth();
    });

    this.$('#model-select').addEventListener('change', async (e) => {
      const selected = e.target.value;
      if (!selected) return;
      await this.switchModel(selected);
    });

    input.addEventListener('input', () => {
      this._emit('bloom:input', { value: input.value });
    });

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.sendPrompt();
      }
    });

    input.addEventListener('paste', (e) => {
      const items = (e.clipboardData || e.originalEvent?.clipboardData)?.items;
      if (!items) return;
      for (const item of items) {
        if (item.kind !== 'file') continue;
        const f = item.getAsFile();
        if (f) this._addAttachmentFile(f);
      }
    });

    ['dragenter', 'dragover'].forEach((evtName) => {
      dropZone.addEventListener(evtName, (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
      });
    });
    ['dragleave', 'drop'].forEach((evtName) => {
      dropZone.addEventListener(evtName, (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
      });
    });
    dropZone.addEventListener('drop', (e) => {
      const files = e.dataTransfer?.files;
      if (files && files.length) {
        Array.from(files).forEach((f) => this._addAttachmentFile(f));
      }
    });
  }

  _emit(name, detail = {}) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  _saveTabPrefs() {
    try {
      const payload = {
        activeSessionId: this._activeSessionId,
        tabMeta: this._sessions.map((s) => ({ id: s.id, label: s.label })),
      };
      localStorage.setItem(this._storageKey('tabs'), JSON.stringify(payload));
    } catch {}
  }

  _loadTabPrefs() {
    try {
      const raw = localStorage.getItem(this._storageKey('tabs'));
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  async init() {
    await Promise.all([this.loadInfo(), this.loadModels()]);
    await this.loadSessionTabs();
  }

  async refresh() {
    const icon = this.$('#btn-refresh .ico');
    if (icon) icon.classList.add('ico-spin');
    try {
      await Promise.allSettled([this.loadInfo(), this.loadModels(), this.loadSessionTabs()]);
    } finally {
      if (icon) icon.classList.remove('ico-spin');
    }
  }

  async loadInfo() {
    const dot = this.$('#conn-dot');
    const text = this.$('#conn-text');
    try {
      const info = await this._api.getInfo();
      this._info = info;
      this._connected = info?.status === 'ready';
      if (dot) dot.className = this._connected ? 'status-dot' : 'status-dot offline';
      if (text) text.textContent = this._connected ? 'Agent: Connected' : 'Agent: Offline';
      this.$('#auth-bar')?.classList.remove('visible');
      this._updateTitleDom();
      this._emit('bloom:status', { online: this._connected, info });
    } catch (err) {
      this._connected = false;
      if (dot) dot.className = 'status-dot offline';
      if (text) text.textContent = err.status === 401 ? 'Auth Required' : 'Agent: Offline';
      if (err instanceof BloomApiError && err.status === 401) {
        this.$('#auth-bar')?.classList.add('visible');
        this.$('#btn-auth-toggle')?.classList.remove('hidden');
      }
      this._emit('bloom:status', { online: false, error: err.message });
    }
  }

  async loadModels() {
    const select = this.$('#model-select');
    if (!select) return;
    try {
      const data = await this._api.getModels();
      const groups = Array.isArray(data?.groups) ? data.groups : [];
      const current = data?.current || data?.active || '';
      select.innerHTML = '';

      if (!groups.length) {
        const opt = document.createElement('option');
        opt.value = current || '';
        opt.textContent = current || 'No models configured';
        select.appendChild(opt);
      } else {
        for (const grp of groups) {
          const optgroup = document.createElement('optgroup');
          optgroup.label = grp.provider || 'default';
          for (const m of (grp.models || [])) {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id;
            if (m.id === current) opt.selected = true;
            optgroup.appendChild(opt);
          }
          select.appendChild(optgroup);
        }
      }
      if (current) select.value = current;
      this._emit('bloom:models', data);
    } catch {
      select.innerHTML = '<option value="">Unavailable</option>';
    }
  }

  async switchModel(model) {
    try {
      const res = await this._api.switchModel(model);
      const cur = res?.current || model;
      const sel = this.$('#model-select');
      if (sel) sel.value = cur;
      const sObj = this._sessions.find((s) => s.id === this._activeSessionId);
      if (sObj) sObj.model = cur;
      this._appendChatMessage('system', `Active model switched to: **${cur}**`);
      this._emit('bloom:model-change', { model: cur });
      return true;
    } catch (err) {
      this._appendChatMessage('system', `Failed to switch model: ${err.message}`);
      await this.loadModels();
      return false;
    }
  }

  async loadSessionTabs() {
    try {
      const [serverList, activeRes] = await Promise.all([
        this._api.listSessions(),
        this._api.getActiveSessions().catch(() => ({ data: {} })),
      ]);
      const activeMap = activeRes?.data || {};
      const prefs = this._loadTabPrefs();
      const savedMeta = new Map((prefs?.tabMeta || []).map((t) => [t.id, t]));

      let list = Array.isArray(serverList) ? serverList : [];
      if (!list.length) {
        const sid = `ses_${Date.now()}`;
        await this._api.createSession(sid);
        list = [{ id: sid, title: '(empty)' }];
      }

      // Preserve order of previously known tabs while adding any new server sessions
      const byId = new Map(list.map((item) => [item.id, item]));
      const ordered = [];
      if (prefs?.tabMeta) {
        for (const meta of prefs.tabMeta) {
          if (byId.has(meta.id)) {
            ordered.push(byId.get(meta.id));
            byId.delete(meta.id);
          }
        }
      }
      for (const item of byId.values()) {
        ordered.push(item);
      }

      this._sessions = ordered.map((item, idx) => {
        const meta = savedMeta.get(item.id);
        this._tabCounter = Math.max(this._tabCounter, idx + 1);
        const rawTitle = item.title && item.title !== '(empty)' ? item.title : '';
        return {
          id: item.id,
          label: meta?.label || `Chat ${idx + 1}`,
          title: rawTitle,
          isBusy: Boolean(activeMap[item.id] || this._sessionState.get(item.id)?.isThinking),
        };
      });

      const preferred = prefs?.activeSessionId;
      if (!this._activeSessionId || !this._sessions.some((s) => s.id === this._activeSessionId)) {
        this._activeSessionId =
          preferred && this._sessions.some((s) => s.id === preferred)
            ? preferred
            : this._sessions[0].id;
      }

      this._saveTabPrefs();
      this._renderTabs();
      this._updateSessionBadge(this._activeSessionId);
      this._renderTokenBadge(this._sessionUsage.get(this._activeSessionId));
      await this.loadChatHistory(this._activeSessionId);
      this._emit('bloom:session-change', { sessionId: this._activeSessionId });
    } catch (err) {
      if (err instanceof BloomApiError && err.status === 401) {
        this.$('#auth-bar')?.classList.add('visible');
      }
    }
  }

  async createSessionTab() {
    const btn = this.$('#btn-new-tab');
    if (btn) btn.disabled = true;
    try {
      const sid = `ses_${Date.now()}`;
      await this._api.createSession(sid);
      this._saveCurrentDraft();
      this._tabCounter = Math.max(this._tabCounter + 1, this._sessions.length + 1);
      this._sessions.unshift({
        id: sid,
        label: `Chat ${this._tabCounter}`,
        title: '',
        isBusy: false,
      });
      this._activeSessionId = sid;
      this._attachments = [];
      this._renderAttachmentPreviews();
      this.$('#chat-input').value = '';
      this._setThinking(false);
      this._saveTabPrefs();
      this._renderTabs();
      this._updateSessionBadge(sid);
      this._renderTokenBadge(null);
      await this.loadChatHistory(sid);
      this._emit('bloom:session-change', { sessionId: sid });
      return sid;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async switchSessionTab(targetId) {
    if (!targetId || targetId === this._activeSessionId) return;
    this._saveCurrentDraft();
    this._stopHistoryPoller();

    this._activeSessionId = targetId;
    this._saveTabPrefs();
    this._renderTabs();
    this._updateSessionBadge(targetId);
    this._renderTokenBadge(this._sessionUsage.get(targetId));

    const entry = this._sessionState.get(targetId) || {};
    this.$('#chat-input').value = entry.draftInput || '';
    this._attachments = entry.draftAttachments ? [...entry.draftAttachments] : [];
    this._renderAttachmentPreviews();
    this._setThinking(Boolean(entry.isThinking));

    await this.loadChatHistory(targetId);
    this._emit('bloom:session-change', { sessionId: targetId });
  }

  async closeSessionTab(targetId) {
    if (!targetId) return;
    this._closedIds.add(targetId);

    const entry = this._sessionState.get(targetId);
    if (entry?.abortController) {
      try { entry.abortController.abort(); } catch {}
    }
    this._sessionState.delete(targetId);
    this._sessionUsage.delete(targetId);

    const wasActive = this._activeSessionId === targetId;
    this._sessions = this._sessions.filter((s) => s.id !== targetId);

    try {
      await this._api.deleteSession(targetId);
    } catch {}

    if (!this._sessions.length) {
      const sid = `ses_${Date.now()}`;
      await this._api.createSession(sid).catch(() => {});
      this._tabCounter += 1;
      this._sessions.push({ id: sid, label: `Chat ${this._tabCounter}`, title: '', isBusy: false });
      this._activeSessionId = sid;
    } else if (wasActive) {
      this._activeSessionId = this._sessions[0].id;
      this._stopHistoryPoller();
    }

    this._closedIds.delete(targetId);
    this._saveTabPrefs();
    this._renderTabs();
    this._updateSessionBadge(this._activeSessionId);
    this._setThinking(Boolean(this._sessionState.get(this._activeSessionId)?.isThinking));
    if (wasActive || this._sessions.length === 1) {
      await this.loadChatHistory(this._activeSessionId);
    }
    this._emit('bloom:session-change', { sessionId: this._activeSessionId });
  }

  async resetActiveSession() {
    const sid = this._activeSessionId;
    if (!sid) return;
    if (!confirm('Reset current chat session? This tab will start fresh and previous context will be cleared.')) {
      return;
    }
    const btn = this.$('#btn-reset');
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `${ICONS.spinner}<span>Resetting...</span>`;

    const entry = this._sessionState.get(sid);
    if (entry?.abortController) {
      try { entry.abortController.abort(); } catch {}
    }
    this._sessionState.delete(sid);
    this._sessionUsage.delete(sid);
    this._stopHistoryPoller();
    this._setThinking(false);

    try {
      await this._api.abortSession(sid).catch(() => {});
      await this._api.deleteSession(sid).catch(() => {});
      await this._api.createSession(sid);
      this._attachments = [];
      this._renderAttachmentPreviews();
      this.$('#chat-input').value = '';
      const tab = this._sessions.find((s) => s.id === sid);
      if (tab) tab.isBusy = false;
      this._renderTabs();
      this._renderTokenBadge(null);
      await this.loadChatHistory(sid);
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }

  _saveCurrentDraft() {
    if (!this._activeSessionId) return;
    const entry = this._sessionState.get(this._activeSessionId) || {};
    entry.draftInput = this.$('#chat-input')?.value || '';
    entry.draftAttachments = [...this._attachments];
    this._sessionState.set(this._activeSessionId, entry);
  }

  _renderTabs() {
    const listEl = this.$('#tabs-list');
    if (!listEl) return;
    listEl.innerHTML = '';

    for (const s of this._sessions) {
      const isActive = s.id === this._activeSessionId;
      const entry = this._sessionState.get(s.id);
      const isBusy = Boolean(entry?.isThinking || s.isBusy);

      const tab = document.createElement('div');
      tab.className = `tab-item${isActive ? ' active' : ''}`;
      tab.title = s.title || s.label;
      tab.addEventListener('click', () => this.switchSessionTab(s.id));

      const dot = document.createElement('span');
      dot.className = `tab-dot${isBusy ? ' busy' : ''}`;
      dot.dataset.tabDot = s.id;

      const label = document.createElement('span');
      label.className = 'tab-label';
      label.textContent = s.label;

      tab.appendChild(dot);
      tab.appendChild(label);

      if (this._sessions.length > 1) {
        const closeBtn = document.createElement('button');
        closeBtn.className = 'tab-close';
        closeBtn.innerHTML = '&times;';
        closeBtn.title = 'Close tab';
        closeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.closeSessionTab(s.id);
        });
        tab.appendChild(closeBtn);
      }

      listEl.appendChild(tab);
      if (isActive) {
        requestAnimationFrame(() => {
          tab.scrollIntoView({ inline: 'nearest', block: 'nearest' });
        });
      }
    }
  }

  _updateTabBusyStatus(sessionId, isBusy) {
    const sObj = this._sessions.find((s) => s.id === sessionId);
    if (sObj) sObj.isBusy = isBusy;
    const dot = this.shadowRoot.querySelector(`[data-tab-dot="${cssEscape(sessionId)}"]`);
    if (dot) dot.className = `tab-dot${isBusy ? ' busy' : ''}`;
  }

  _updateSessionBadge(sessionId) {
    const badge = this.$('#session-badge');
    if (badge) {
      badge.textContent = `ID: ${sessionId || '--'}`;
      badge.title = `Active Session: ${sessionId || '--'}`;
    }
  }

  _updateSessionUsage(sessionId, usage) {
    if (!sessionId || !usage) return;
    const prev = this._sessionUsage.get(sessionId) || {};
    const merged = {
      ...prev,
      ...usage,
      contextTokens: usage.contextTokens !== undefined ? Number(usage.contextTokens) : (prev.contextTokens || 0),
      contextLimit: usage.contextLimit !== undefined ? Number(usage.contextLimit) : (prev.contextLimit || 0),
      promptTokens: usage.promptTokens !== undefined ? Number(usage.promptTokens) : (prev.promptTokens || 0),
      candidatesTokens: usage.candidatesTokens !== undefined ? Number(usage.candidatesTokens) : (prev.candidatesTokens || 0),
      thoughtsTokens: usage.thoughtsTokens !== undefined ? Number(usage.thoughtsTokens) : (usage.thoughtsTokens || 0),
      totalTokens: usage.totalTokens !== undefined ? Number(usage.totalTokens) : (prev.totalTokens || 0),
      cachedTokens: usage.cachedTokens !== undefined ? Number(usage.cachedTokens) : (prev.cachedTokens || 0),
    };
    delete merged.type;
    this._sessionUsage.set(sessionId, merged);
    if (sessionId === this._activeSessionId) {
      this._renderTokenBadge(merged);
    }
  }

  _renderTokenBadge(usage) {
    const badge = this.$('#token-badge');
    const textEl = this.$('#token-text');
    if (!badge || !textEl) return;

    if (!usage || (!usage.contextTokens && !usage.totalTokens)) {
      textEl.textContent = 'Tokens: --';
      badge.className = 'token-badge';
      badge.title = 'No token usage recorded yet for this session.';
      return;
    }

    const ctx = Number(usage.contextTokens) || 0;
    const limit = Number(usage.contextLimit) || 0;
    const pct = limit > 0 ? Math.min(100, Math.round((ctx / limit) * 100)) : 0;
    textEl.textContent = limit > 0
      ? `Context: ${formatTokenCount(ctx)} / ${formatTokenCount(limit)} (${pct}%)`
      : `Context: ${formatTokenCount(ctx)}`;

    let tone = 'tone-ok';
    if (pct >= 80) tone = 'tone-danger';
    else if (pct >= 60) tone = 'tone-warn';
    badge.className = `token-badge ${tone}`;

    badge.title = [
      `Active Context: ${ctx.toLocaleString()} / ${limit > 0 ? limit.toLocaleString() : '--'} tokens (${pct}%)`,
      `----------------------------------------`,
      `Session Metered:`,
      `  • Prompt Tokens: ${(usage.promptTokens || 0).toLocaleString()}`,
      `  • Output Tokens: ${(usage.candidatesTokens || 0).toLocaleString()}`,
      `  • Reasoning: ${(usage.thoughtsTokens || 0).toLocaleString()}`,
      `  • Cached Tokens: ${(usage.cachedTokens || 0).toLocaleString()}`,
      `  • Total Metered: ${(usage.totalTokens || 0).toLocaleString()}`,
    ].join('\n');
  }

  _mergeHistoryEntries(entries, isBusy) {
    const messages = [];
    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      const parts = Array.isArray(entry.content) ? entry.content : [];
      const text = [];
      const reasoning = [];
      const tools = [];
      const attachments = [];
      const role = entry.role || 'assistant';

      for (const part of parts) {
        if (part.type === 'text' && typeof part.text === 'string') {
          let cleanText = part.text;
          if (role === 'user') {
            cleanText = cleanText.replace(/^\[Attachment:\s*([^\n]+?)\s*\((file:\/\/[^\s)]+)\)\]$/gm, (_, name, uri) => {
              attachments.push({ name: name.trim(), path: uri.replace(/^file:\/\//, ''), url: uri });
              return '';
            }).trim();
          }
          if (cleanText) text.push(cleanText);
        } else if (part.type === 'reasoning' && typeof part.text === 'string') {
          reasoning.push(part.text);
        } else if (part.type === 'tool') {
          tools.push({
            id: part.callId || part.id || null,
            callId: part.callId || part.id || null,
            tool: part.name || part.tool || 'tool',
            input: part.input,
            output: part.output,
            status: part.state || part.status || 'completed',
          });
        }
      }

      const serialized = {
        id: entry.id || `msg-${i}`,
        role,
        text: text.join('\n\n'),
        reasoning: reasoning.join('\n\n'),
        tools,
        attachments,
        isBusy: role === 'assistant' ? Boolean(isBusy && i === entries.length - 1) : false,
      };

      const prev = messages[messages.length - 1];
      if (serialized.role !== 'assistant' || prev?.role !== 'assistant') {
        messages.push(serialized);
        continue;
      }

      prev.text = [prev.text, serialized.text].filter((t) => t && t.trim()).join('\n\n');
      prev.reasoning = [prev.reasoning, serialized.reasoning].filter((t) => t && t.trim()).join('\n\n');
      for (const t of serialized.tools) {
        const key = t.callId || t.id;
        const idx = key ? prev.tools.findIndex((item) => (item.callId || item.id) === key) : -1;
        if (idx >= 0) prev.tools[idx] = t;
        else prev.tools.push(t);
      }
    }

    const last = messages[messages.length - 1];
    if (last?.role === 'assistant') last.isBusy = Boolean(isBusy);
    return messages;
  }

  async loadChatHistory(targetSessionId = null) {
    const sId = targetSessionId || this._activeSessionId;
    if (!sId) return;

    try {
      const [msgRes, activeRes] = await Promise.all([
        this._api.getSessionMessages(sId),
        this._api.getActiveSessions().catch(() => ({ data: {} })),
      ]);

      const container = this.$('#messages');
      if (!container || sId !== this._activeSessionId || this._closedIds.has(sId)) return;

      if (msgRes?.usage) {
        this._updateSessionUsage(sId, msgRes.usage);
      }

      const activeMap = activeRes?.data || {};
      const stateEntry = this._sessionState.get(sId) || {};
      const serverBusy = Boolean(activeMap[sId] || stateEntry.isThinking);
      const entries = Array.isArray(msgRes?.data) ? msgRes.data : [];
      const merged = this._mergeHistoryEntries(entries, serverBusy);

      const hasLiveBubble = Boolean(stateEntry.isThinking && stateEntry.abortController && stateEntry.bubble?.wrapper);
      let toRender = merged.filter(
        (m) => (m.text && m.text.trim()) || (m.tools && m.tools.length) || (m.reasoning && m.reasoning.trim()) || (m.attachments && m.attachments.length) || m.isBusy
      );

      if (hasLiveBubble && toRender.length > 0 && toRender[toRender.length - 1].role === 'assistant') {
        toRender = toRender.slice(0, -1);
      }

      if (toRender.length > 0 || hasLiveBubble) {
        container.innerHTML = '';
        toRender.forEach((m, idx) => {
          this._appendChatMessage(m.role, m.text, m.tools, m.attachments, m.reasoning, m.isBusy, `${sId}-${m.id || idx}`);
        });
        if (hasLiveBubble) {
          container.appendChild(stateEntry.bubble.wrapper);
          container.scrollTop = container.scrollHeight;
        }
      } else {
        const agent = escapeHtml(this._agentDisplayName());
        container.innerHTML = `
          <div class="welcome-card">
            <p class="welcome-title">${ICONS.terminal}<span>${agent} ready</span></p>
            <div>Persistent headless session powered by Bloom. Your history and tool logs are saved on disk across reloads.</div>
            <div class="welcome-session-pill">
              <span class="status-dot"></span>
              <span>Active Session: ${escapeHtml(sId)}</span>
            </div>
          </div>
        `;
      }

      stateEntry.isThinking = serverBusy;
      this._sessionState.set(sId, stateEntry);
      this._updateTabBusyStatus(sId, serverBusy);
      this._setThinking(serverBusy);
      if (serverBusy) {
        this._startHistoryPoller(sId);
      } else {
        this._stopHistoryPoller();
      }
      await this._restorePendingPrompts(sId, container, stateEntry.bubble);
    } catch (err) {
      if (err instanceof BloomApiError && err.status === 404) {
        const container = this.$('#messages');
        if (container && sId === this._activeSessionId) {
          container.innerHTML = `
            <div class="msg-card" style="border-color:rgba(244,63,94,0.4);color:#fda4af;font-family:var(--bloom-font-mono);font-size:11px;">
              <strong>Session not found:</strong> <code>${escapeHtml(sId)}</code> no longer exists on the server. Switch tabs or click Reset Chat.
            </div>
          `;
        }
      }
    }
  }

  _stopHistoryPoller() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
    this._pollSessionId = null;
  }

  _startHistoryPoller(targetId) {
    const sId = targetId || this._activeSessionId;
    if (!sId || sId !== this._activeSessionId || this._closedIds.has(sId)) return;
    if (this._pollTimer && this._pollSessionId === sId) return;
    this._stopHistoryPoller();
    this._pollSessionId = sId;
    let inFlight = false;

    const timer = setInterval(async () => {
      if (inFlight || sId !== this._activeSessionId || this._pollTimer !== timer) return;
      inFlight = true;
      try {
        const [msgRes, activeRes] = await Promise.all([
          this._api.getSessionMessages(sId),
          this._api.getActiveSessions().catch(() => ({ data: {} })),
        ]);
        const container = this.$('#messages');
        if (!container || sId !== this._activeSessionId || this._pollTimer !== timer) return;

        if (msgRes?.usage) this._updateSessionUsage(sId, msgRes.usage);
        const busy = Boolean(activeRes?.data?.[sId]);
        const entry = this._sessionState.get(sId) || {};
        const hasLiveBubble = Boolean(entry.isThinking && entry.abortController && entry.bubble?.wrapper);

        if (!hasLiveBubble) {
          const merged = this._mergeHistoryEntries(msgRes?.data || [], busy);
          const renderable = merged.filter(
            (m) => (m.text && m.text.trim()) || (m.tools && m.tools.length) || (m.reasoning && m.reasoning.trim()) || m.isBusy
          );
          if (renderable.length) {
            container.innerHTML = '';
            renderable.forEach((m, idx) => {
              this._appendChatMessage(m.role, m.text, m.tools, m.attachments, m.reasoning, m.isBusy, `${sId}-${m.id || idx}`);
            });
          }
        }

        entry.isThinking = busy;
        this._sessionState.set(sId, entry);
        this._updateTabBusyStatus(sId, busy);
        this._setThinking(busy);
        if (!busy) this._stopHistoryPoller();
        await this._restorePendingPrompts(sId, container, entry.bubble);
      } catch {
      } finally {
        inFlight = false;
      }
    }, 1500);

    this._pollTimer = timer;
  }

  async _backgroundStatusTick() {
    if (document.hidden || !this._sessions.length) return;
    try {
      const res = await this._api.getActiveSessions();
      const activeMap = res?.data || {};
      for (const s of this._sessions) {
        const entry = this._sessionState.get(s.id);
        const localBusy = Boolean(entry?.isThinking && entry?.abortController);
        const busy = Boolean(activeMap[s.id] || localBusy);
        this._updateTabBusyStatus(s.id, busy);
      }
    } catch {}
  }

  async _restorePendingPrompts(sessionId, container, bubble) {
    if (!container || sessionId !== this._activeSessionId || this._closedIds.has(sessionId)) return;
    try {
      const data = await this._api.getPermissions(sessionId);
      const perms = (Array.isArray(data?.pending) ? data.pending : []).filter((p) => p && p.sessionId === sessionId);
      const pendingIds = new Set(perms.map((p) => String(p.requestId)));
      container.querySelectorAll('[data-permission-id]').forEach((card) => {
        if (card.dataset.sessionId === sessionId && !pendingIds.has(card.dataset.permissionId)) {
          card.remove();
        }
      });

      let host = container.querySelector(`[data-pending-prompts][data-session-id="${cssEscape(sessionId)}"]`);
      if (!host && perms.length) {
        host = bubble?.promptHost || document.createElement('div');
        host.setAttribute('data-pending-prompts', '');
        host.dataset.sessionId = sessionId;
        if (!container.contains(host)) container.appendChild(host);
      }
      for (const perm of perms) {
        if (!perm.requestId) continue;
        if (container.querySelector(`[data-permission-id="${cssEscape(perm.requestId)}"]`)) continue;
        host.appendChild(this._createPermissionCard(perm, sessionId));
      }
    } catch {}
  }

  _createPermissionCard(perm, sessionId) {
    const resources = Array.isArray(perm.resources) ? perm.resources : [];
    const card = document.createElement('div');
    card.className = 'perm-card';
    if (perm.requestId) card.dataset.permissionId = perm.requestId;
    card.dataset.sessionId = sessionId;
    card.innerHTML = `
      <div class="perm-title">${ICONS.shield}<span>Permission required</span></div>
      <div class="perm-action">${escapeHtml(perm.action || `Execute ${perm.tool || 'tool'}`)}</div>
      ${resources.length ? `<div class="perm-resources">${resources.map((r) => escapeHtml(r)).join('<br>')}</div>` : ''}
      <div class="perm-buttons">
        <button data-perm="approved" class="btn-allow">Allow</button>
        <button data-perm="rejected" class="btn-reject">Reject</button>
        <span data-perm-status class="perm-status"></span>
      </div>
    `;

    const statusEl = card.querySelector('[data-perm-status]');
    card.querySelectorAll('[data-perm]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const decision = btn.getAttribute('data-perm');
        card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        statusEl.textContent = 'Sending...';
        try {
          const data = await this._api.replyApproval(sessionId, perm.requestId, decision);
          const approved = Boolean(data?.approved);
          statusEl.style.color = approved ? '#34d399' : '#fb7185';
          statusEl.textContent = approved ? 'Allowed' : 'Rejected';
          this._startHistoryPoller(sessionId);
          setTimeout(() => { if (card.parentNode) card.remove(); }, 2500);
        } catch (err) {
          if (err instanceof BloomApiError && (err.status === 404 || err.status === 410)) {
            statusEl.textContent = 'Expired';
            return;
          }
          card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
          statusEl.style.color = '#fb7185';
          statusEl.textContent = err.message;
        }
      });
    });

    return card;
  }

  _renderToolItem(tool, isOpen = false, keyPrefix = 'm') {
    const tName = tool.tool || tool.name || 'tool';
    const tStatus = tool.status || tool.state || 'completed';
    const input = tool.input ?? tool.arguments ?? null;
    const output = tool.output ?? null;

    let icon = ICONS.check;
    let iconCls = 'ico-done';
    let badgeCls = 'badge-done';
    let statusText = 'done';

    if (tStatus === 'running') {
      icon = ICONS.spinner;
      iconCls = 'ico-running';
      badgeCls = 'badge-running';
      statusText = 'running...';
    } else if (tStatus === 'failed' || tStatus === 'error') {
      icon = ICONS.warn;
      iconCls = 'ico-failed';
      badgeCls = 'badge-failed';
      statusText = 'failed';
    } else if (tStatus === 'stopped' || tStatus === 'aborted' || tStatus === 'cancelled') {
      icon = ICONS.ban;
      iconCls = 'ico-stopped';
      badgeCls = '';
      statusText = tStatus;
    }

    let previewSnippet = '';
    if (input) {
      if (typeof input === 'string') previewSnippet = input;
      else if (input.command) previewSnippet = input.command;
      else if (input.path) previewSnippet = String(input.path).split('/').pop() || input.path;
      else if (input.pattern) previewSnippet = input.pattern;
      else if (input.name) previewSnippet = input.name;
      else if (input.query) previewSnippet = input.query;
      else if (input.skill_name) previewSnippet = input.skill_name;
    }

    let inputHtml = '';
    if (input !== null && input !== undefined) {
      let inputStr = '';
      if (typeof input === 'string') inputStr = input;
      else if (input.command) inputStr = input.command;
      else {
        try { inputStr = JSON.stringify(input, null, 2); }
        catch { inputStr = String(input); }
      }
      if (inputStr && inputStr !== '{}') {
        inputHtml = `
          <div>
            <div class="io-label">${ICONS.arrowIn}<span>${input?.command ? 'Command' : 'Input / Parameters'}:</span></div>
            <pre class="io-pre">${escapeHtml(inputStr)}</pre>
          </div>
        `;
      }
    }

    let outputHtml = '';
    if (output !== null && output !== undefined && output !== '') {
      let outStr = typeof output === 'string' ? output : JSON.stringify(output, null, 2);
      outputHtml = `
        <div>
          <div class="io-label">${ICONS.arrowOut}<span>Output:</span></div>
          <pre class="io-pre">${escapeHtml(outStr)}</pre>
        </div>
      `;
    } else if (tStatus === 'running') {
      outputHtml = `<div style="color:#71717a;font-style:italic;font-size:10px;display:flex;align-items:center;gap:6px;"><span class="ico-running">${ICONS.spinner}</span> Running tool execution...</div>`;
    }

    const key = tool.callId || tool.id || tName;
    const hasDetails = Boolean(inputHtml || outputHtml);
    const openKey = `${keyPrefix}-tool-${key}`;
    const shouldOpen = isOpen || Boolean(this._detailsOpen.get(openKey));

    return `
      <details class="tool-item" data-tool-key="${escapeHtml(key)}" data-open-key="${escapeHtml(openKey)}" ${shouldOpen ? 'open' : ''}>
        <summary>
          <div class="tool-left">
            <span class="${iconCls}">${icon}</span>
            <span class="tool-name">${escapeHtml(tName)}</span>
            <span class="badge ${badgeCls}">${escapeHtml(statusText)}</span>
            ${previewSnippet ? `<span class="tool-preview" title="${escapeHtml(previewSnippet)}">${escapeHtml(previewSnippet)}</span>` : ''}
          </div>
          ${hasDetails ? ICONS.chevronRight : ''}
        </summary>
        ${hasDetails ? `<div class="tool-body">${inputHtml}${outputHtml}</div>` : ''}
      </details>
    `;
  }

  _renderToolsContainer(tools, isOpen = false, openToolKeys = new Set(), keyPrefix = 'm') {
    if (!tools || !tools.length) return '';
    const doneCount = tools.filter((t) => ['completed', 'done'].includes(t.status || t.state || 'completed')).length;
    const runningCount = tools.filter((t) => (t.status || t.state) === 'running').length;
    const statusBadge = runningCount > 0
      ? `<span class="sum-count-running pulse">(${runningCount} running...)</span>`
      : `<span class="sum-count-done">(${doneCount}/${tools.length} completed)</span>`;

    const toolsOpenKey = `${keyPrefix}-tools`;
    const mainOpen = isOpen || Boolean(this._detailsOpen.get(toolsOpenKey));

    return `
      <details class="section-box" data-open-key="${escapeHtml(toolsOpenKey)}" ${mainOpen ? 'open' : ''}>
        <summary>
          <div class="sum-left">
            ${ICONS.wrench}
            <span class="sum-title">Tool execution (${tools.length})</span>
            ${statusBadge}
          </div>
          ${ICONS.chevronRight}
        </summary>
        <div class="tools-scroll-list">
          ${tools.map((t) => this._renderToolItem(t, openToolKeys.has(t.callId || t.id || t.tool || ''), keyPrefix)).join('')}
        </div>
      </details>
    `;
  }

  _appendChatMessage(role, text, tools, attachments, reasoning, isBusy = false, msgKey = null) {
    const container = this.$('#messages');
    if (!container) return;
    const key = msgKey || `m${++this._msgCounter}`;

    const hasText = Boolean(text && text.trim());
    const hasTools = Boolean(tools && tools.length > 0);
    const hasReasoning = Boolean(reasoning && reasoning.trim());
    const hasAtts = Boolean(attachments && attachments.length > 0);
    if (!hasText && !hasTools && !hasReasoning && !hasAtts && !isBusy) return;

    const agentLabel = escapeHtml(`${this._agentDisplayName()} agent`);
    const wrapper = document.createElement('div');
    let roleHtml = '';

    if (role === 'user') {
      wrapper.className = 'msg-card msg-user';
      roleHtml = '<div class="role-header"><span>YOU</span></div>';
    } else if (role === 'assistant') {
      wrapper.className = 'msg-card msg-assistant';
      roleHtml = `<div class="role-header"><span class="term-ico">${ICONS.terminal}</span><span>${agentLabel}</span></div>`;
    } else {
      wrapper.className = 'msg-card msg-system';
      roleHtml = '<div class="role-header sys"><span>System</span></div>';
    }

    let attHtml = '';
    if (hasAtts) {
      attHtml = `<div class="att-list-msg">${attachments.map((a) => {
        const name = a.name || 'file';
        const safeSrc = sanitizeSrc(a.previewUrl || a.url);
        if (isInlineImageMime(a.mime) && safeSrc) {
          return `<a href="${safeSrc}" target="_blank" rel="noopener noreferrer" class="att-img-link" title="${escapeHtml(name)}"><img src="${safeSrc}" alt="${escapeHtml(name)}"><span class="att-img-cap">${escapeHtml(name)}</span></a>`;
        }
        return `<span class="att-chip" title="${escapeHtml(a.path || name)}">${ICONS.file}<span>${escapeHtml(name)}</span></span>`;
      }).join('')}</div>`;
    }

    let reasoningHtml = '';
    if (hasReasoning) {
      const rKey = `${key}-reasoning`;
      const rOpen = Boolean(this._detailsOpen.get(rKey));
      reasoningHtml = `
        <details class="section-box" data-open-key="${escapeHtml(rKey)}" ${rOpen ? 'open' : ''}>
          <summary>
            <div class="sum-left">${ICONS.brain}<span class="sum-title">Thought process</span></div>
            ${ICONS.chevronRight}
          </summary>
          <div class="reasoning-body">${escapeHtml(reasoning)}</div>
        </details>
      `;
    }

    const toolHtml = this._renderToolsContainer(tools, false, new Set(), key);

    let bodyHtml = '';
    if (hasText) {
      bodyHtml = role === 'user'
        ? `<div class="prose-chat" style="white-space:pre-wrap">${escapeHtml(text)}</div>`
        : `<div class="prose-chat">${formatMarkdown(text)}</div>`;
    } else if (isBusy) {
      bodyHtml = `<div class="working-line"><span class="status-dot busy"></span><span>${agentLabel} is working...</span></div>`;
    } else if (hasTools) {
      bodyHtml = '<div style="color:#71717a;font-family:var(--bloom-font-mono);font-size:11px;font-style:italic">Completed tool execution.</div>';
    } else {
      bodyHtml = '<div style="color:#71717a;font-family:var(--bloom-font-mono);font-size:11px;font-style:italic">(No response)</div>';
    }

    wrapper.innerHTML = `${roleHtml}${attHtml}${reasoningHtml}${toolHtml}${bodyHtml}`;
    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;
  }

  _createStreamingBubble(sessionId) {
    const container = this.$('#messages');
    const wrapper = document.createElement('div');
    wrapper.className = 'msg-card msg-assistant';
    wrapper.dataset.sessionId = sessionId;

    const agentLabel = escapeHtml(`${this._agentDisplayName()} agent`);
    const prefix = `live-${sessionId}`;

    const roleDiv = document.createElement('div');
    roleDiv.className = 'role-header';
    roleDiv.innerHTML = `<span class="term-ico">${ICONS.terminal}</span><span>${agentLabel}</span>`;

    const reasoningDetails = document.createElement('details');
    reasoningDetails.className = 'section-box hidden';
    reasoningDetails.setAttribute('data-open-key', `${prefix}-reasoning`);
    reasoningDetails.innerHTML = `
      <summary>
        <div class="sum-left">${ICONS.brain}<span class="sum-title">Thought process</span><span class="reasoning-badge">(thinking...)</span></div>
        ${ICONS.chevronRight}
      </summary>
      <div class="reasoning-body"></div>
    `;
    const reasoningBody = reasoningDetails.querySelector('.reasoning-body');
    const reasoningBadge = reasoningDetails.querySelector('.reasoning-badge');

    const toolContainer = document.createElement('div');
    toolContainer.className = 'hidden';

    const promptHost = document.createElement('div');
    promptHost.setAttribute('data-pending-prompts', '');
    promptHost.dataset.sessionId = sessionId;

    const textWrapper = document.createElement('div');
    textWrapper.className = 'stream-raw';

    const cursor = document.createElement('span');
    cursor.className = 'cursor-blink';

    wrapper.append(roleDiv, reasoningDetails, toolContainer, promptHost, textWrapper, cursor);
    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;

    let fullText = '';
    let fullReasoning = '';
    const toolMap = new Map();

    return {
      wrapper,
      promptHost,
      appendDelta: (delta) => {
        fullText += delta;
        textWrapper.textContent = fullText;
        container.scrollTop = container.scrollHeight;
      },
      appendReasoning: (delta) => {
        fullReasoning += delta;
        reasoningDetails.classList.remove('hidden');
        reasoningBody.textContent = fullReasoning;
        reasoningBody.scrollTop = reasoningBody.scrollHeight;
        container.scrollTop = container.scrollHeight;
      },
      updateTool: (toolObj) => {
        const key = toolObj.callId || toolObj.id || toolObj.tool || toolObj.name || String(Math.random());
        const isNew = !toolMap.has(key);
        const existing = toolMap.get(key) || {};
        const merged = { ...existing, ...toolObj };
        toolMap.set(key, merged);

        const openToolKeys = new Set();
        toolContainer.querySelectorAll('details.tool-item[data-tool-key]').forEach((d) => {
          if (d.open) openToolKeys.add(d.getAttribute('data-tool-key'));
        });
        const mainDetails = toolContainer.querySelector('details.section-box');
        const isMainOpen = mainDetails ? mainDetails.open : true;

        const oldList = toolContainer.querySelector('.tools-scroll-list');
        let prevScroll = 0;
        let nearBottom = true;
        if (oldList) {
          prevScroll = oldList.scrollTop;
          nearBottom = (oldList.scrollHeight - oldList.scrollTop - oldList.clientHeight) < 40;
        }

        toolContainer.classList.remove('hidden');
        toolContainer.innerHTML = this._renderToolsContainer(Array.from(toolMap.values()), isMainOpen, openToolKeys, prefix);

        const newList = toolContainer.querySelector('.tools-scroll-list');
        if (newList) {
          newList.scrollTop = (isNew || nearBottom) ? newList.scrollHeight : prevScroll;
        }
        container.scrollTop = container.scrollHeight;
      },
      showPermission: (perm) => {
        if (!perm?.requestId) return;
        if (container.querySelector(`[data-permission-id="${cssEscape(perm.requestId)}"]`)) return;
        promptHost.appendChild(this._createPermissionCard(perm, sessionId));
        container.scrollTop = container.scrollHeight;
      },
      settlePermission: (requestId) => {
        const card = container.querySelector(`[data-permission-id="${cssEscape(requestId)}"]`);
        if (!card) return;
        card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        const st = card.querySelector('[data-perm-status]');
        if (st) { st.textContent = 'Resolved'; st.style.color = '#a1a1aa'; }
        setTimeout(() => { if (card.parentNode) card.remove(); }, 4000);
      },
      getText: () => fullText,
      setText: (txt) => {
        fullText = txt;
        textWrapper.className = 'prose-chat';
        textWrapper.innerHTML = formatMarkdown(fullText);
        container.scrollTop = container.scrollHeight;
      },
      finish: (statusNote = null) => {
        if (cursor.parentNode) cursor.remove();
        textWrapper.className = 'prose-chat';
        if (reasoningBadge) reasoningBadge.remove();
        if (fullReasoning) {
          reasoningDetails.open = false;
          this._detailsOpen.delete(`${prefix}-reasoning`);
        }

        if (statusNote) {
          toolMap.forEach((t) => {
            if ((t.status || t.state) === 'running') {
              t.status = 'stopped';
              t.output = t.output || 'Execution stopped by user.';
            }
          });
        }

        if (toolMap.size > 0) {
          this._detailsOpen.delete(`${prefix}-tools`);
          toolMap.forEach((t) => this._detailsOpen.delete(`${prefix}-tool-${t.callId || t.id || t.tool}`));
          toolContainer.innerHTML = this._renderToolsContainer(Array.from(toolMap.values()), false, new Set(), prefix);
        }

        if (fullText) {
          textWrapper.innerHTML = formatMarkdown(fullText);
        } else if (statusNote) {
          textWrapper.innerHTML = `<span style="color:#71717a;font-style:italic">${escapeHtml(statusNote)}</span>`;
        } else if (!toolMap.size) {
          textWrapper.innerHTML = '<span style="color:#71717a;font-style:italic">(No response)</span>';
        }
        container.scrollTop = container.scrollHeight;
      },
    };
  }

  async _addAttachmentFile(file) {
    if (!file) return;
    if (file.size === 0) {
      this._appendChatMessage('system', `"${file.name}" is empty and was not attached.`);
      return;
    }
    if (file.size > MAX_ATTACH_BYTES) {
      this._appendChatMessage('system', `"${file.name}" (${formatAttachmentSize(file.size)}) exceeds the 25 MB limit.`);
      return;
    }

    const entry = {
      id: `att-${Date.now()}-${++this._attachSeq}`,
      name: file.name || 'attachment',
      mime: file.type || 'application/octet-stream',
      size: file.size,
      path: null,
      uri: null,
      previewUrl: isInlineImageMime(file.type) ? URL.createObjectURL(file) : null,
      uploading: true,
      error: null,
    };
    this._attachments.push(entry);
    this._renderAttachmentPreviews();

    try {
      const res = await this._api.attachFile(file);
      entry.path = res.path;
      entry.uri = res.uri || `file://${res.path}`;
      entry.mime = res.mime || entry.mime;
      entry.size = typeof res.size === 'number' ? res.size : entry.size;
    } catch (err) {
      entry.error = err.message || 'Upload failed';
    } finally {
      entry.uploading = false;
      this._renderAttachmentPreviews();
    }
  }

  _removeAttachment(idx) {
    const [removed] = this._attachments.splice(idx, 1);
    if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
    this._renderAttachmentPreviews();
  }

  _renderAttachmentPreviews() {
    const bar = this.$('#att-preview');
    if (!bar) return;
    if (!this._attachments.length) {
      bar.innerHTML = '';
      bar.classList.add('hidden');
      return;
    }
    bar.classList.remove('hidden');
    bar.innerHTML = '';

    this._attachments.forEach((att, idx) => {
      const card = document.createElement('div');
      card.className = `att-preview-card${att.error ? ' err' : ''}`;
      card.title = att.name;

      const safePrev = isInlineImageMime(att.mime) ? sanitizeSrc(att.previewUrl) : '';
      const thumbHtml = safePrev
        ? `<img src="${safePrev}" class="att-thumb" alt="preview">`
        : `<span class="att-thumb-ico">${ICONS.file}</span>`;
      const subHtml = att.uploading
        ? '<span class="att-meta-sub">uploading...</span>'
        : att.error
          ? `<span class="att-meta-err" title="${escapeHtml(att.error)}">failed</span>`
          : `<span class="att-meta-sub">${escapeHtml(formatAttachmentSize(att.size))}</span>`;

      card.innerHTML = `
        ${thumbHtml}
        <span class="att-meta">
          <span class="att-meta-name">${escapeHtml(att.name)}</span>
          ${subHtml}
        </span>
      `;
      const rmBtn = document.createElement('button');
      rmBtn.type = 'button';
      rmBtn.className = 'att-remove';
      rmBtn.innerHTML = '&times;';
      rmBtn.title = 'Remove attachment';
      rmBtn.addEventListener('click', () => this._removeAttachment(idx));
      card.appendChild(rmBtn);
      bar.appendChild(card);
    });
  }

  getActiveSessionId() {
    return this._activeSessionId;
  }

  isBusy() {
    return Boolean(this._activeSessionId && this._sessionState.get(this._activeSessionId)?.isThinking);
  }

  getInputValue() {
    return this.$('#chat-input')?.value || '';
  }

  setInputValue(val, { focus = false } = {}) {
    const input = this.$('#chat-input');
    if (!input) return;
    input.value = String(val ?? '');
    this._emit('bloom:input', { value: input.value });
    if (focus) this.focusInput();
  }

  focusInput() {
    const input = this.$('#chat-input');
    if (!input) return;
    input.focus();
    const len = input.value.length;
    input.setSelectionRange(len, len);
  }

  appendSystemMessage(text) {
    this._appendChatMessage('system', text);
  }

  async _handleSlashCommand(rawText) {
    const sid = this._activeSessionId;
    const parts = rawText.trim().split(/\s+/);
    const cmd = parts[0].toLowerCase();

    if (cmd === '/help') {
      this.appendSystemMessage([
        '**Available commands:**',
        '- `/help` — show available commands',
        '- `/compact` — summarize older turns to free context window',
        '- `/rewind [index]` — jump back to an earlier point in this chat',
        '- `/sessions` — refresh and list saved sessions',
      ].join('\n'));
      return;
    }

    if (cmd === '/compact') {
      if (!sid) return;
      this.appendSystemMessage('Compacting older context...');
      this._updateTabBusyStatus(sid, true);
      this._setThinking(true);
      try {
        const res = await this._api.compactSession(sid);
        if (res?.status === 'compacted') {
          if (res.usage) this._updateSessionUsage(sid, res.usage);
          await this.loadChatHistory(sid);
          this.appendSystemMessage(`Compacted history: **${res.before_count} -> ${res.after_count}** messages.`);
        } else {
          this.appendSystemMessage(res?.reason || 'Nothing old enough to compact yet.');
        }
      } catch (err) {
        this.appendSystemMessage(`Compaction failed: ${err.message}`);
      } finally {
        this._updateTabBusyStatus(sid, false);
        this._setThinking(false);
      }
      return;
    }

    if (cmd === '/sessions') {
      await this.loadSessionTabs();
      const lines = this._sessions.map((s) => `- \`${s.id}\`${s.title ? ` — ${s.title}` : ''}`);
      this.appendSystemMessage(`**Sessions (${this._sessions.length}):**\n${lines.join('\n')}`);
      return;
    }

    if (cmd === '/rewind') {
      if (!sid) return;
      const arg = parts[1];
      if (arg && /^\d+$/.test(arg)) {
        try {
          const res = await this._api.rewindSession(sid, Number(arg));
          if (res?.usage) this._updateSessionUsage(sid, res.usage);
          await this.loadChatHistory(sid);
          this.appendSystemMessage(`Rewound to index **${res.targetIndex}** (${res.remaining_messages} messages remaining).`);
        } catch (err) {
          this.appendSystemMessage(`Rewind failed: ${err.message}`);
        }
        return;
      }
      try {
        const cands = await this._api.getRewindCandidates(sid);
        if (!Array.isArray(cands) || !cands.length) {
          this.appendSystemMessage('Nothing to rewind in this session.');
          return;
        }
        const container = this.$('#messages');
        const card = document.createElement('div');
        card.className = 'msg-card msg-system';
        card.innerHTML = '<div class="role-header sys"><span>Rewind to checkpoint</span></div>';
        const list = document.createElement('div');
        list.style.cssText = 'display:flex;flex-direction:column;gap:4px;margin-top:6px;';
        for (const c of cands) {
          const btn = document.createElement('button');
          btn.className = 'btn';
          btn.style.cssText = 'justify-content:flex-start;text-align:left;width:100%;height:auto;padding:5px 8px;white-space:normal;';
          btn.innerHTML = `<span style="color:#34d399">[${c.target_index}]</span> <strong>${escapeHtml(c.role)}:</strong> <span>${escapeHtml(c.preview || '')}</span>`;
          btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
              const res = await this._api.rewindSession(sid, c.target_index);
              if (res?.usage) this._updateSessionUsage(sid, res.usage);
              await this.loadChatHistory(sid);
              this.appendSystemMessage(`Rewound to checkpoint **${c.target_index}**.`);
            } catch (err) {
              this.appendSystemMessage(`Rewind failed: ${err.message}`);
            }
          });
          list.appendChild(btn);
        }
        card.appendChild(list);
        container?.appendChild(card);
        if (container) container.scrollTop = container.scrollHeight;
      } catch (err) {
        this.appendSystemMessage(`Failed to load rewind checkpoints: ${err.message}`);
      }
      return;
    }

    this.appendSystemMessage(`Unknown command \`${escapeHtml(cmd)}\`. Type \`/help\` for available commands.`);
  }

  _setThinking(val) {
    const ind = this.$('#thinking-indicator');
    const sendBtn = this.$('#btn-send');
    const stopBtn = this.$('#btn-stop');
    if (ind) ind.classList.toggle('hidden', !val);
    if (sendBtn) sendBtn.classList.toggle('hidden', val);
    if (stopBtn) stopBtn.classList.toggle('hidden', !val);
  }

  async sendPrompt(customText = null, { files = null, displayText = null } = {}) {
    if (!this._activeSessionId) {
      await this.createSessionTab();
    }
    const sid = this._activeSessionId;
    if (!sid) return;
    const state = this._sessionState.get(sid) || {};
    if (state.isThinking) return;

    const input = this.$('#chat-input');
    const message = customText !== null ? String(customText).trim() : (input?.value || '').trim();

    if (this._attachments.some((a) => a.uploading)) {
      this._appendChatMessage('system', 'Please wait for attachments to finish uploading.');
      return;
    }
    if (this._attachments.some((a) => a.error || !a.path)) {
      this._appendChatMessage('system', 'Remove failed attachments before sending.');
      return;
    }

    const activeAtts = [...this._attachments];
    const promptFiles = files || activeAtts.map((a) => ({
      name: a.name,
      uri: a.uri || `file://${a.path}`,
    }));
    if (!message && !promptFiles.length) return;

    if (customText === null && message.startsWith('/')) {
      if (input) {
        input.value = '';
        this._emit('bloom:input', { value: '' });
      }
      await this._handleSlashCommand(message);
      return;
    }

    if (customText === null && input) {
      input.value = '';
      this._emit('bloom:input', { value: '' });
    }
    this._attachments = [];
    this._renderAttachmentPreviews();

    // Remove welcome card on first message
    const container = this.$('#messages');
    const welcome = container?.querySelector('.welcome-card');
    if (welcome) welcome.remove();

    this._appendChatMessage('user', (displayText && String(displayText).trim()) || message || '(Attachment)', null, activeAtts);

    state.isThinking = true;
    const controller = new AbortController();
    state.abortController = controller;
    const bubble = this._createStreamingBubble(sid);
    state.bubble = bubble;
    this._sessionState.set(sid, state);
    this._updateTabBusyStatus(sid, true);
    this._setThinking(true);

    const model = this.$('#model-select')?.value || undefined;
    let finished = false;
    let errorMsg = null;
    let aborted = false;

    try {
      const res = await this._api.streamPrompt(
        sid,
        { text: message || 'See attached files.', model, files: promptFiles },
        {
          signal: controller.signal,
          onEvent: (ev) => {
            const viewing = this._activeSessionId === sid;
            if (ev.type === 'delta') {
              if (!viewing) return;
              if (ev.field === 'reasoning') bubble.appendReasoning(ev.text);
              else bubble.appendDelta(ev.text);
            } else if (ev.type === 'tool') {
              if (viewing) bubble.updateTool(ev);
              if ((ev.state === 'completed' || ev.status === 'completed')) {
                this._emit('bloom:tool-done', ev);
              }
            } else if (ev.type === 'permission') {
              bubble.showPermission(ev);
            } else if (ev.type === 'permission_settled') {
              bubble.settlePermission(ev.requestId);
            } else if (ev.type === 'usage') {
              this._updateSessionUsage(sid, ev);
            } else if (ev.type === 'error') {
              const msg = typeof ev.error === 'object' ? (ev.error.message || JSON.stringify(ev.error)) : ev.error;
              if (viewing) this._appendChatMessage('system', `Error: ${msg}`);
            }
          },
        }
      );
      finished = res.finished;
      errorMsg = res.errorMessage;
    } catch (err) {
      if (err.name === 'AbortError') {
        aborted = true;
      } else {
        errorMsg = err.message;
        if (this._activeSessionId === sid) {
          this._appendChatMessage('system', `Request failed: ${err.message}`);
        }
      }
    } finally {
      state.abortController = null;
      state.isThinking = false;
      state.bubble = null;
      this._sessionState.set(sid, state);
      this._updateTabBusyStatus(sid, false);
      if (this._activeSessionId === sid) {
        this._setThinking(false);
        this._stopHistoryPoller();
      }

      if (aborted) {
        bubble.finish('(Stopped by user)');
      } else if (errorMsg) {
        bubble.finish(`(Failed: ${errorMsg})`);
        this._emit('bloom:error', { sessionId: sid, error: errorMsg });
      } else {
        bubble.finish();
        this._emit('bloom:turn-done', { sessionId: sid, finished });
      }
    }
  }

  async abortActiveTurn() {
    const sid = this._activeSessionId;
    if (!sid) return;
    const entry = this._sessionState.get(sid);
    if (entry?.abortController) {
      try { entry.abortController.abort(); } catch {}
      entry.abortController = null;
    }
    if (entry?.bubble) {
      entry.bubble.finish('(Stopped by user)');
      entry.bubble = null;
    }
    if (entry) {
      entry.isThinking = false;
      this._sessionState.set(sid, entry);
    }
    this._updateTabBusyStatus(sid, false);
    this._stopHistoryPoller();
    this._setThinking(false);
    await this._api.abortSession(sid).catch(() => {});
  }
}

if (!customElements.get('bloom-chat')) {
  customElements.define('bloom-chat', BloomChatElement);
}
