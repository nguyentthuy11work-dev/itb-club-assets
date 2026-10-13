// ============================================================================
// API CLIENT
// ----------------------------------------------------------------------------
// Lớp fetch dùng chung cho toàn app. Khi có backend thật, chỉ cần:
//   1. Set VITE_API_BASE_URL trong file .env (copy từ .env.example)
//   2. Đảm bảo backend trả JSON đúng "hợp đồng" mô tả trong src/api/services.js
// Không cần sửa gì ở các trang (pages/) hay component — chúng chỉ gọi qua
// services.js, không gọi fetch trực tiếp.
// ============================================================================

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1';

export const USE_MOCK =
  import.meta.env.VITE_USE_MOCK === 'true' ||
  (!import.meta.env.VITE_API_BASE_URL && import.meta.env.VITE_USE_MOCK !== 'false');

const TOKEN_KEY = 'clb_assets_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

/**
 * Gọi backend thật qua fetch.
 * @param {string} path - ví dụ '/auth/login'
 * @param {object} options - { method, body, params }
 */
export async function request(path, { method = 'GET', body, params, headers: extraHeaders } = {}) {
  let url = `${BASE_URL}${path}`;
  if (params && Object.keys(params).length) {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString();
    if (qs) url += `?${qs}`;
  }

  const isFormData = body instanceof FormData;
  const headers = { ...(isFormData ? {} : { 'Content-Type': 'application/json' }), ...extraHeaders };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: 'include',
      body: body !== undefined ? (isFormData ? body : JSON.stringify(body)) : undefined,
    });
  } catch (networkErr) {
    throw new ApiError('Không thể kết nối tới máy chủ. Kiểm tra kết nối mạng hoặc backend.', 0, null);
  }

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const message =
      (payload && (payload.message || payload.error?.message)) ||
      `Yêu cầu thất bại (mã ${response.status})`;
    throw new ApiError(message, response.status, payload);
  }

  return payload;
}

export async function download(path) {
  const token = getToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  let response;
  try {
    response = await fetch(`${BASE_URL}${path}`, { headers, credentials: 'include' });
  } catch {
    throw new ApiError('Không thể kết nối tới máy chủ.', 0, null);
  }
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch { /* response may not be JSON */ }
    throw new ApiError(payload?.error?.message || `Yêu cầu thất bại (mã ${response.status})`, response.status, payload);
  }
  const disposition = response.headers.get('Content-Disposition') || '';
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] || 'handover.csv';
  return { blob: await response.blob(), filename };
}

export const http = {
  get: (path, params) => request(path, { method: 'GET', params }),
  post: (path, body) => request(path, { method: 'POST', body }),
  put: (path, body) => request(path, { method: 'PUT', body }),
  patch: (path, body) => request(path, { method: 'PATCH', body }),
  del: (path) => request(path, { method: 'DELETE' }),
  postIdempotent: (path, body, idempotencyKey) => request(path, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
  }),
  patchIdempotent: (path, body, idempotencyKey) => request(path, {
    method: 'PATCH',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
  }),
  postMultipart: (path, body, idempotencyKey) => request(path, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
  }),
};
