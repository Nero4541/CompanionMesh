// One shared connection to the server's live events (/v1/admin/events) for
// the pages that want to react at once (a device connecting, leaving, being
// set up or updated) instead of waiting for their next poll.

import { getToken } from "./api.js";

const listeners = new Set();
let socket = null;
let retry = null;
let delay = 1000;

function connect() {
  retry = null;
  const token = getToken();
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  socket = new WebSocket(`${scheme}://${window.location.host}/v1/admin/events${query}`);
  socket.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    delay = 1000;
    if (!data.event) return; // the backlog ("recent") is for the Events page
    for (const listener of listeners) listener(data.event);
  };
  socket.onclose = () => {
    socket = null;
    if (!listeners.size) return;
    retry = setTimeout(connect, delay);
    delay = Math.min(delay * 2, 15000);
  };
}

// Call `listener(event)` for each new event whose type starts with one of
// `prefixes`; returns a function that stops it.
export function onEvents(prefixes, listener) {
  const wrapped = (event) => {
    if (prefixes.some((p) => event.type.startsWith(p))) listener(event);
  };
  listeners.add(wrapped);
  if (!socket && !retry) connect();
  return () => {
    listeners.delete(wrapped);
    if (!listeners.size) {
      clearTimeout(retry);
      retry = null;
      socket?.close();
      socket = null;
    }
  };
}

// Device comings and goings, setup and updates.
export const DEVICE_EVENTS = [
  "notice.device_",
  "session.devices",
  "session.started",
  "session.ended",
];
