// Admin API client. The admin token (COMPANION_ADMIN_TOKEN) is kept in this
// browser only; without one the server admits requests from its own machine.

const TOKEN_KEY = "companion.admin.token";

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function getToken() {
  return storage()?.getItem(TOKEN_KEY) || null;
}

export function setToken(token) {
  const s = storage();
  if (!s) return;
  if (token) s.setItem(TOKEN_KEY, token);
  else s.removeItem(TOKEN_KEY);
}

// Open the page once as /admin/?token=... to store the token, then drop it
// from the address bar.
export function takeTokenFromUrl() {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (!token) return;
  setToken(token);
  url.searchParams.delete("token");
  window.history.replaceState(null, "", url);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }

  get isAuth() {
    return this.status === 401 || this.status === 403;
  }
}

async function request(path, options) {
  const headers = { ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(`/v1/admin${path}`, { ...options, headers });
  } catch (err) {
    throw new ApiError(0, `server unreachable (${err.message})`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // not JSON
    }
    throw new ApiError(res.status, detail);
  }
  return res;
}

export async function api(path, options = {}) {
  return (await request(path, options)).json();
}

// For images (camera frames): an object URL the caller revokes when done.
export async function apiImage(path, options = {}) {
  const blob = await (await request(path, options)).blob();
  return URL.createObjectURL(blob);
}
