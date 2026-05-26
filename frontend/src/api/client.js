/* Fetch-wrapper — session-cookies via credentials:'include', 401 → /login.
 *
 * En mekanism för att registrera en callback som triggas vid 401. Registreras
 * av App-shell:en så den kan navigera utan att api-klienten känner till
 * routerns internals.
 */

const BASE = import.meta.env.VITE_API_BASE || '';

let unauthorizedHandler = () => {
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    window.location.href = '/login';
  }
};

export function setUnauthorizedHandler(handler) {
  if (typeof handler === 'function') {
    unauthorizedHandler = handler;
  }
}

export class ApiError extends Error {
  constructor(message, { status, body } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const { method = 'GET', body, headers: customHeaders, signal } = options;
  const headers = { Accept: 'application/json', ...(customHeaders || {}) };
  let payload;
  if (body instanceof FormData || body == null) {
    payload = body;
  } else if (typeof body === 'string') {
    payload = body;
  } else {
    headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    payload = JSON.stringify(body);
  }

  const resp = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: payload,
    credentials: 'include',
    signal,
  });

  if (resp.status === 204) return null;

  const contentType = resp.headers.get('content-type') || '';
  const isJson = contentType.includes('application/json');
  const data = isJson ? await resp.json().catch(() => null) : await resp.text();

  if (resp.status === 401) {
    // Skilj på "ej inloggad" och "OAuth-token utgången" — det senare
    // signaleras av {auth_required: 'gmail'|'drive'} i bodyn och ska
    // INTE trigga redirect till /login.
    const authRequired = data && typeof data === 'object' ? data.auth_required : null;
    if (!authRequired) {
      unauthorizedHandler();
      throw new ApiError('Not authenticated', { status: 401, body: data });
    }
    const message =
      (data && typeof data === 'object' && data.detail) ||
      `${authRequired} requires reconnect`;
    const err = new ApiError(message, { status: 401, body: data });
    err.authRequired = authRequired;
    throw err;
  }

  if (!resp.ok) {
    const message =
      (data && typeof data === 'object' && (data.detail || data.message)) ||
      `${resp.status} ${resp.statusText}`;
    throw new ApiError(message, { status: resp.status, body: data });
  }

  return data;
}

export const api = {
  me: () => request('/api/me'),
  stats: () => request('/api/stats'),
  messages: (limit = 50) => request(`/api/messages?limit=${limit}`),
  runs: (limit = 20) => request(`/api/runs?limit=${limit}`),
  settings: () => request('/api/settings'),
  updateSettings: (payload) => request('/api/settings', { method: 'PUT', body: payload }),
  scan: () => request('/api/scan', { method: 'POST' }),
  uploadToBezala: (id, overrides) =>
    request(`/api/messages/${id}/upload-to-bezala`, {
      method: 'POST',
      body: overrides || {},
    }),
  bezalaMissingReceipts: () => request('/api/bezala/missing-receipts'),
  bezalaMatchSuggestions: () => request('/api/bezala/match-suggestions'),
  bezalaMatchSuggestionsAll: () =>
    request('/api/bezala/match-suggestions?include_all_messages=true'),
  matchHealth: ({ refresh = false } = {}) =>
    request(`/api/debug/match-health${refresh ? '?refresh=true' : ''}`),
  matchToBezala: (msgId, missingReceiptId) =>
    request(`/api/messages/${msgId}/match-to-bezala`, {
      method: 'POST',
      body: { missing_receipt_id: missingReceiptId },
    }),
  // C33 / FAS 7a — manuell upload av fysiskt kvitto. file = File-objekt.
  // billLineId valfri — om satt kopplas raden direkt till Bezala-kortraden.
  // paymentMethod = 'company_card' (default) eller 'private_card' (stub).
  uploadManualReceipt: ({
    file,
    paymentMethod = 'company_card',
    comment = '',
    billLineId = null,
  }) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('payment_method', paymentMethod);
    if (comment) fd.append('comment', comment);
    if (billLineId != null) fd.append('bill_line_id', String(billLineId));
    return request('/api/messages/upload', { method: 'POST', body: fd });
  },
  deleteErrors: () => request('/api/messages/errors', { method: 'DELETE' }),
  trashList: (limit = 200) => request(`/api/messages/trash?limit=${limit}`),
  trashCount: () => request('/api/messages/trash/count'),
  softDeleteMessage: (id, reason = 'manual') =>
    request(`/api/messages/${id}`, { method: 'DELETE', body: { reason } }),
  hardDeleteMessage: (id, { purgeDrive = false } = {}) =>
    request(
      `/api/messages/${id}?permanent=true&purge_drive=${purgeDrive ? 'true' : 'false'}`,
      { method: 'DELETE' },
    ),
  restoreMessage: (id) =>
    request(`/api/messages/${id}/restore`, { method: 'POST' }),
  bulkDelete: ({ ids, reason = 'manual', permanent = false, purge_drive = false }) =>
    request('/api/messages/bulk-delete', {
      method: 'POST',
      body: { ids, reason, permanent, purge_drive },
    }),
  emptyTrash: ({ purgeDrive = false } = {}) =>
    request(
      `/api/messages/trash?purge_drive=${purgeDrive ? 'true' : 'false'}`,
      { method: 'DELETE' },
    ),
  fetchPdf: (id) =>
    request(`/api/messages/${id}/fetch-pdf`, { method: 'POST' }),
  reprocessMessage: (id) =>
    request(`/api/messages/${id}/reprocess`, { method: 'POST' }),
  reprocessMessageFull: (id, { force = false } = {}) =>
    request(
      `/api/messages/${id}/reprocess-full${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),
  reprocessGmailWindow: ({ days = 30, vendorFilter = null, maxResults = 100 } = {}) =>
    request('/api/gmail/reprocess', {
      method: 'POST',
      body: {
        days,
        vendor_filter: vendorFilter || null,
        max_results: maxResults,
      },
    }),
  cleanupExcludedVendors: () =>
    request('/api/trips/cleanup-excluded-vendors', { method: 'POST' }),
  messageBody: (id) => request(`/api/messages/${id}/body`),
  fetchPdfFromUrl: (id, url) =>
    request(`/api/messages/${id}/fetch-pdf-from-url`, {
      method: 'POST',
      body: { url },
    }),
  feedbackThumbs: ({ messageId, isPositive, fields = [] }) =>
    request('/api/feedback/thumbs', {
      method: 'POST',
      body: { message_id: messageId, is_positive: isPositive, fields },
    }),
  feedbackCorrection: ({ messageId, fieldName, aiValue, correctValue }) =>
    request('/api/feedback/correction', {
      method: 'POST',
      body: {
        message_id: messageId,
        field_name: fieldName,
        ai_value: aiValue,
        correct_value: correctValue,
      },
    }),
  feedbackNotAReceipt: ({ messageId }) =>
    request('/api/feedback/not-a-receipt', {
      method: 'POST',
      body: { message_id: messageId },
    }),
  feedbackMatchResult: ({
    messageId,
    billLineId = null,
    result,
    aiScore = null,
    scoreBreakdown = null,
  }) =>
    request('/api/feedback/match-result', {
      method: 'POST',
      body: {
        message_id: messageId,
        bill_line_id: billLineId,
        result,
        ai_score: aiScore,
        score_breakdown: scoreBreakdown,
      },
    }),
  feedbackStats: () => request('/api/feedback/stats'),
  // FAS 8.5 — Travel Tinder Matchade-vy
  matchedPairs: ({ period = '30d', search = '' } = {}) => {
    const qp = new URLSearchParams({ period });
    const s = (search || '').trim();
    if (s) qp.set('search', s);
    return request(`/api/bezala/matched-pairs?${qp.toString()}`);
  },
  unmatchReceipt: (messageId) =>
    request(`/api/bezala/unmatch/${encodeURIComponent(messageId)}`, {
      method: 'POST',
    }),
  // FAS 11.1 — Resor
  tripsSuggestions: () => request('/api/trips/suggestions'),
  tripsActive: () => request('/api/trips/active'),
  tripsStats: () => request('/api/trips/stats'),
  tripsGet: (id) => request(`/api/trips/${id}`),
  tripsAccept: (id) => request(`/api/trips/${id}/accept`, { method: 'POST' }),
  tripsReject: (id) => request(`/api/trips/${id}/reject`, { method: 'POST' }),
  tripsEdit: (id, payload) =>
    request(`/api/trips/${id}`, { method: 'PATCH', body: payload }),
  tripsArchive: (id) =>
    request(`/api/trips/${id}`, { method: 'DELETE' }),
  tripsRefreshSuggestions: () =>
    request('/api/trips/refresh-suggestions', { method: 'POST' }),
  tripsFeedback: (id, payload) =>
    request(`/api/trips/${id}/feedback`, { method: 'POST', body: payload }),
  // FAS 11.1.1 — manuell tagging + SaaS-lista
  tripsAvailableForMessage: (messageId) =>
    request(`/api/messages/${encodeURIComponent(messageId)}/available-trips`),
  tripsLinkMessage: (messageId, tripId) =>
    request(`/api/messages/${encodeURIComponent(messageId)}/link-to-trip`, {
      method: 'POST',
      body: { trip_id: tripId },
    }),
  tripsUnlinkMessage: (messageId, tripId) =>
    request(
      `/api/messages/${encodeURIComponent(messageId)}/unlink-from-trip/${tripId}`,
      { method: 'DELETE' },
    ),
  excludedVendorsList: () => request('/api/excluded-vendors'),
  excludedVendorsAdd: (payload) =>
    request('/api/excluded-vendors', { method: 'POST', body: payload }),
  excludedVendorsRemove: (id) =>
    request(`/api/excluded-vendors/${id}`, { method: 'DELETE' }),
  bezalaConfigList: () => request('/api/bezala-config'),
  bezalaConfigCreate: (payload) =>
    request('/api/bezala-config', { method: 'POST', body: payload }),
  bezalaConfigUpdate: (id, payload) =>
    request(`/api/bezala-config/${id}`, { method: 'PATCH', body: payload }),
  bezalaConfigDelete: (id) =>
    request(`/api/bezala-config/${id}`, { method: 'DELETE' }),
  htmlOnlySendersList: () => request('/api/settings/html-only-senders'),
  htmlOnlySendersAdd: (payload) =>
    request('/api/settings/html-only-senders', {
      method: 'POST', body: payload,
    }),
  htmlOnlySendersRemove: (id) =>
    request(`/api/settings/html-only-senders/${id}`, { method: 'DELETE' }),
  htmlOnlySendersToggle: (id, isActive) =>
    request(`/api/settings/html-only-senders/${id}`, {
      method: 'PATCH', body: { is_active: isActive },
    }),
  // FAS 11.5.1 — Per Diem
  tripsExtractFlightTimes: (id) =>
    request(`/api/trips/${id}/extract-flight-times`, { method: 'POST' }),
  tripsCalculatePerDiem: (id, payload) =>
    request(`/api/trips/${id}/calculate-per-diem`, {
      method: 'POST',
      body: payload,
    }),
  tripsGetPerDiem: (id) => request(`/api/trips/${id}/per-diem`),
  tripsUpdatePerDiem: (id, payload) =>
    request(`/api/trips/${id}/per-diem`, { method: 'PATCH', body: payload }),
  perDiemRates: (year) =>
    request(`/api/per-diem-rates${year ? `?year=${year}` : ''}`),
  logout: async () => {
    await fetch(`${BASE}/logout`, { method: 'POST', credentials: 'include' });
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
  },
};

export { request };
