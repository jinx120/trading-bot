import { useEffect, useRef, useState } from "react";

/**
 * Subscribes to /ws/live for position + score updates.
 * The server pushes a JSON payload every 5s. Components can read the
 * shared state via the returned object.
 */
export type LivePayload = {
  ts: string;
  account: any;
  positions: any[];
  scores: { symbols: any[]; weights: Record<string, number> };
};

const initial: LivePayload = {
  ts: "", account: {}, positions: [], scores: { symbols: [], weights: {} },
};

let _shared: LivePayload = initial;
let _subscribers: Array<(p: LivePayload) => void> = [];
let _ws: WebSocket | null = null;
let _connected = false;
let _connListeners: Array<(c: boolean) => void> = [];

function _connect() {
  if (_ws) return;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  // Dev: Vite proxy forwards ws to FastAPI. Prod: same origin.
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`);
  _ws = ws;
  ws.onopen = () => {
    _connected = true;
    _connListeners.forEach(fn => fn(true));
    ws.send("hello");  // keepalive ping (server waits on receive_text)
  };
  ws.onmessage = (e) => {
    try {
      _shared = JSON.parse(e.data);
      _subscribers.forEach(fn => fn(_shared));
    } catch {}
  };
  const onClose = () => {
    _ws = null;
    _connected = false;
    _connListeners.forEach(fn => fn(false));
    setTimeout(_connect, 3000);
  };
  ws.onclose = onClose;
  ws.onerror = onClose;
}

export function useLive() {
  const [data, setData] = useState<LivePayload>(_shared);
  const [connected, setConnected] = useState<boolean>(_connected);
  const idRef = useRef<((p: LivePayload) => void) | null>(null);
  const cRef = useRef<((c: boolean) => void) | null>(null);

  useEffect(() => {
    idRef.current = (p) => setData(p);
    cRef.current = (c) => setConnected(c);
    _subscribers.push(idRef.current);
    _connListeners.push(cRef.current);
    _connect();
    return () => {
      _subscribers = _subscribers.filter(f => f !== idRef.current);
      _connListeners = _connListeners.filter(f => f !== cRef.current);
    };
  }, []);

  return { data, connected };
}
