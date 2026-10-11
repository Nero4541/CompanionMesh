// Waiting for the server to come back after a restart.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Where the dashboard lives after the restart, given the new host and port.
export function nextOrigin({ host, port }) {
  const here = window.location;
  const anyAddress = ["0.0.0.0", "::", ""].includes(host);
  const loopback = ["127.0.0.1", "localhost", "::1"];
  const sameHost =
    anyAddress || host === here.hostname || (loopback.includes(host) && loopback.includes(here.hostname));
  const hostname = sameHost ? here.hostname : host.includes(":") ? `[${host}]` : host;
  return `${here.protocol}//${hostname}:${port}`;
}

// Resolves once `origin` answers /health again (true), or after `timeoutMs` (false).
// no-cors: the new origin may differ from this page's, and reaching it is enough.
export async function waitForServer(origin, timeoutMs = 120_000) {
  await sleep(1500); // the server stops half a second after answering
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await fetch(`${origin}/health`, { mode: "no-cors", cache: "no-store" });
      return true;
    } catch {
      await sleep(1000);
    }
  }
  return false;
}
