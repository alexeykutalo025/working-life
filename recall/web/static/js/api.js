// Talking to the local server.
//
// Every failure surfaces as an ApiError carrying the server's own plain-language
// message. Nothing here invents a friendlier story than the one the server told.

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch (err) {
    throw new ApiError(
      'Recall is not answering. The black window that started it may have been ' +
      'closed. Start it again with start.bat.',
      0,
      String(err),
    );
  }

  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { detail: text };
    }
  }

  if (!response.ok) {
    const detail = body && (body.detail || body.error);
    throw new ApiError(
      typeof detail === 'string' ? detail : `The server replied ${response.status}.`,
      response.status,
      body,
    );
  }
  return body;
}

export const api = {
  get: (path) => request(path),
  post: (path, data) => request(path, { method: 'POST', body: JSON.stringify(data ?? {}) }),

  health: () => request('/api/health'),
  home: () => request('/api/home'),

  // --- sources ---
  scanTargets: () => request('/api/scan/targets'),
  browseFolders: (path) =>
    request('/api/folders' + (path ? `?path=${encodeURIComponent(path)}` : '')),
  startScan: (roots, fullHash = false) =>
    request('/api/scan', { method: 'POST', body: JSON.stringify({ roots, full_hash: fullHash }) }),
  job: () => request('/api/job'),

  extractPlan: (ids) =>
    request('/api/extract/plan' + (ids && ids.length ? `?ids=${ids.join(',')}` : '')),
  startExtract: (options) =>
    request('/api/extract', { method: 'POST', body: JSON.stringify(options) }),

  cancelJob: () => request('/api/job/cancel', { method: 'POST' }),

  sources: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, v);
    });
    const q = qs.toString();
    return request('/api/sources' + (q ? `?${q}` : ''));
  },
  sourcesSummary: () => request('/api/sources/summary'),
  source: (id) => request(`/api/sources/${id}`),
  setSourceNote: (id, note) =>
    request(`/api/sources/${id}/note`, { method: 'POST', body: JSON.stringify({ note }) }),

  hydratePlan: (ids) =>
    request('/api/sources/hydrate/plan', { method: 'POST', body: JSON.stringify({ ids }) }),
  hydrate: (ids) =>
    request('/api/sources/hydrate', {
      method: 'POST',
      body: JSON.stringify({ ids, confirm: true }),
    }),

  // --- timeline, eras, exports ---
  timeline: (level = 'year', year = null, month = null) => {
    const qs = new URLSearchParams({ level });
    if (year !== null && year !== undefined) qs.set('year', year);
    if (month !== null && month !== undefined) qs.set('month', month);
    return request(`/api/timeline?${qs}`);
  },
  eras: () => request('/api/eras'),
  createEra: (era) =>
    request('/api/eras', { method: 'POST', body: JSON.stringify(era) }),
  deleteEra: (id) => request(`/api/eras/${id}`, { method: 'DELETE' }),

  // --- search and items ---
  search: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, v);
    });
    const q = qs.toString();
    return request('/api/search' + (q ? `?${q}` : ''));
  },
  searchFilters: () => request('/api/search/filters'),
  item: (id) => request(`/api/items/${id}`),
  indexHealth: () => request('/api/index/health'),
  buildIndex: (rebuild = false) =>
    request('/api/index', { method: 'POST', body: JSON.stringify({ rebuild }) }),

  // --- people ---
  people: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, v);
    });
    const q = qs.toString();
    return request('/api/people' + (q ? `?${q}` : ''));
  },
  person: (id) => request(`/api/people/${id}`),
  mergeQueue: () => request('/api/people/merge-queue'),
  mergePeople: (keepId, mergeId, note) =>
    request('/api/people/merge', {
      method: 'POST',
      body: JSON.stringify({ keep_id: keepId, merge_id: mergeId, note }),
    }),
  unmergePerson: (id) =>
    request(`/api/people/${id}/unmerge`, { method: 'POST' }),
  editPerson: (id, fields) =>
    request(`/api/people/${id}`, { method: 'POST', body: JSON.stringify(fields) }),

  // --- findings ---
  findingsSummary: () => request('/api/findings/summary'),
  findings: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, v);
    });
    const q = qs.toString();
    return request('/api/findings' + (q ? `?${q}` : ''));
  },
  coverageMap: () => request('/api/coverage-map'),
  retryFinding: (id) => request(`/api/findings/${id}/retry`, { method: 'POST' }),
  setFindingState: (id, state, note) =>
    request(`/api/findings/${id}/state`, {
      method: 'POST',
      body: JSON.stringify({ state, note }),
    }),

  exportFormats: () => request('/api/export/formats'),
  exportData: (format, kind = 'calendar', full = true) =>
    request('/api/export', {
      method: 'POST',
      body: JSON.stringify({ format, kind, full }),
    }),
  exportSearch: (params) =>
    request('/api/export/search', { method: 'POST', body: JSON.stringify(params) }),
};
