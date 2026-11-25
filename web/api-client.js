/**
 * Bloom HTTP/1.1 & SSE API Client SDK.
 * Pure, dependency-free browser client strictly targeting bloom/server.py endpoints.
 */

export class BloomApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = 'BloomApiError';
    this.status = status;
    this.data = data;
  }
}

export function normalizeApiBase(rawBase) {
  const clean = String(rawBase || '').trim().replace(/\/+$/, '');
  if (!clean) return '/api';
  if (/^https?:\/\/[^/]+$/i.test(clean)) return `${clean}/api`;
  return clean;
}

export class BloomApiClient {
  constructor({ apiBase = '/api', token = '', agent = '' } = {}) {
    this.apiBase = normalizeApiBase(apiBase);
    this.token = (token || '').trim();
    this.agent = (agent || '').trim();
  }

  setToken(token) {
    this.token = (token || '').trim();
  }

  setApiBase(apiBase) {
    this.apiBase = normalizeApiBase(apiBase);
  }

  url(route) {
    const cleanRoute = String(route || '').replace(/^\/+/, '');
    return `${this.apiBase}/${cleanRoute}`;
  }

  headers(extra = {}) {
    const out = { ...extra };
    if (this.token) {
      out.Authorization = `Bearer ${this.token}`;
    }
    if (this.agent) {
      out['X-Agent'] = this.agent;
    }
    return out;
  }

  async request(route, options = {}) {
    const hasJsonBody = options.body !== undefined && !options.rawBody;
    const hdrs = this.headers(
      hasJsonBody
        ? { 'Content-Type': 'application/json', ...(options.headers || {}) }
        : (options.headers || {})
    );

    const res = await fetch(this.url(route), {
      method: options.method || 'GET',
      headers: hdrs,
      body: options.rawBody ? options.body : (hasJsonBody ? JSON.stringify(options.body) : undefined),
      signal: options.signal,
    });

    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }

    if (!res.ok) {
      const msg = (data && typeof data === 'object' && (data.error || data.detail))
        || (typeof data === 'string' && data)
        || `HTTP ${res.status}`;
      throw new BloomApiError(msg, res.status, data);
    }

    return data;
  }

  getInfo(signal) {
    return this.request('/info', { signal });
  }

  getModels(signal) {
    return this.request('/model', { signal });
  }

  switchModel(model, signal) {
    return this.request('/model', { method: 'POST', body: { model }, signal });
  }

  getTools(signal) {
    return this.request('/tools', { signal });
  }

  getSkills(signal) {
    return this.request('/skills', { signal });
  }

  listSessions(signal) {
    return this.request('/sessions', { signal });
  }

  getActiveSessions(signal) {
    return this.request('/session/active', { signal });
  }

  createSession(id = null, signal) {
    const body = id ? { id } : {};
    return this.request('/session', { method: 'POST', body, signal });
  }

  getSessionMessages(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/message`, { signal });
  }

  getSessionUsage(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/usage`, { signal });
  }

  deleteSession(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}`, { method: 'DELETE', signal });
  }

  compactSession(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/compact`, { method: 'POST', body: {}, signal });
  }

  getRewindCandidates(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/rewind-candidates`, { signal });
  }

  rewindSession(sessionId, targetIndex, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/rewind`, {
      method: 'POST',
      body: { targetIndex },
      signal,
    });
  }

  getPermissions(sessionId = '', signal) {
    const q = sessionId ? `?sessionId=${encodeURIComponent(sessionId)}` : '';
    return this.request(`/permissions${q}`, { signal });
  }

  replyApproval(sessionId, requestId, decision, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/approval`, {
      method: 'POST',
      body: { requestId, decision },
      signal,
    });
  }

  abortSession(sessionId, signal) {
    return this.request(`/session/${encodeURIComponent(sessionId)}/abort`, {
      method: 'POST',
      body: {},
      signal,
    });
  }

  attachFile(file, signal) {
    const mime = file.type || 'application/octet-stream';
    const name = file.name || 'attachment';
    return this.request('/attach', {
      method: 'POST',
      rawBody: true,
      headers: {
        'Content-Type': mime,
        'X-Filename': encodeURIComponent(name),
      },
      body: file,
      signal,
    });
  }

  async streamPrompt(sessionId, { text, model, files } = {}, { signal, onEvent } = {}) {
    const payload = { text };
    if (model) payload.model = model;
    if (Array.isArray(files) && files.length) payload.files = files;

    const res = await fetch(this.url(`/session/${encodeURIComponent(sessionId)}/prompt`), {
      method: 'POST',
      headers: this.headers({
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      }),
      body: JSON.stringify(payload),
      signal,
    });

    if (!res.ok) {
      const errText = await res.text();
      let parsed = null;
      try { parsed = JSON.parse(errText); } catch {}
      const msg = (parsed && parsed.error) || errText || `HTTP ${res.status}`;
      throw new BloomApiError(msg, res.status, parsed);
    }

    if (!res.body) {
      throw new BloomApiError('Server returned an empty SSE stream.', 502);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finished = false;
    let errorMessage = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line || line.startsWith(':')) continue;
        if (!line.startsWith('data:')) continue;
        const jsonStr = line.slice(5).trim();
        if (!jsonStr) continue;

        let ev;
        try {
          ev = JSON.parse(jsonStr);
        } catch {
          continue;
        }

        if (ev.type === 'ping') continue;
        if (onEvent) onEvent(ev);
        if (ev.type === 'error') {
          errorMessage = typeof ev.error === 'object'
            ? (ev.error.message || JSON.stringify(ev.error))
            : ev.error;
        }
        if (ev.type === 'done') {
          finished = true;
          try { await reader.cancel(); } catch {}
          break;
        }
      }
      if (finished) break;
    }

    return { finished, errorMessage };
  }
}
