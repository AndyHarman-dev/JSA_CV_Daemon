import { useStore } from "./store";
import type { WSEvent } from "./types";

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let intentionalClose = false;

function clearReconnectTimer() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

export function connectWS(): void {
  // Guard against double-connect: only skip if socket is open or connecting
  if (
    socket !== null &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  intentionalClose = false;
  clearReconnectTimer();

  const store = useStore.getState();
  store.setWsStatus("connecting");

  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);
  socket = ws;

  ws.onopen = () => {
    useStore.getState().setWsStatus("open");
  };

  ws.onmessage = (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data as string) as WSEvent;
      useStore.getState().applyEvent(data);
    } catch (err) {
      console.error("WS message parse error:", err);
    }
  };

  ws.onclose = () => {
    if (socket === ws) {
      socket = null;
    }
    useStore.getState().setWsStatus("closed");
    useStore
      .getState()
      .refetchAll()
      .catch((err: unknown) => {
        console.error("refetchAll on ws close failed:", err);
      });
    // Schedule reconnect after 3 seconds, but only if not intentionally closed
    clearReconnectTimer();
    if (!intentionalClose) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWS();
      }, 3000);
    }
  };

  ws.onerror = (event: Event) => {
    console.error("WebSocket error:", event);
  };
}

export function disconnectWS(): void {
  intentionalClose = true;
  clearReconnectTimer();
  if (socket !== null) {
    socket.close();
    socket = null;
  }
}
