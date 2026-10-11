import { api } from "./api.js";

// Fetch an admin endpoint now and every `intervalMs` while the page shows it.
// Use inside a component: const status = poll("/status", 5000).
export function poll(path, intervalMs, onAuthError) {
  const state = $state({ data: null, error: null, loading: true, updated: null });

  async function refresh() {
    try {
      state.data = await api(typeof path === "function" ? path() : path);
      state.error = null;
      state.updated = new Date();
    } catch (err) {
      state.error = err;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      state.loading = false;
    }
  }

  $effect(() => {
    refresh();
    if (!intervalMs) return;
    const timer = setInterval(() => {
      if (!document.hidden) refresh();
    }, intervalMs);
    return () => clearInterval(timer);
  });

  return { state, refresh };
}
