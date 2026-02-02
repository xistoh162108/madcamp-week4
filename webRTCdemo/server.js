const WebSocket = require("ws");
const WebSocketServer = WebSocket.Server;

const wss = new WebSocketServer({ port: 8080 });

// roomId -> Set<ws>
const rooms = new Map();

function safeJsonParse(raw) {
  try {
    return JSON.parse(raw.toString());
  } catch {
    return null;
  }
}

function ensureRoom(room) {
  if (!rooms.has(room)) rooms.set(room, new Set());
  return rooms.get(room);
}

wss.on("connection", (ws) => {
  ws.on("message", (msg) => {
    const m = safeJsonParse(msg);
    if (!m) return;
    const hasSdp = typeof m.sdp === "string";
    const sdpInfo = hasSdp
      ? {
          sdpLen: m.sdp.length,
          sdpHasLf: m.sdp.indexOf("\n") !== -1,
          sdpHasCr: m.sdp.indexOf("\r") !== -1,
          sdpHasEsc: m.sdp.indexOf("\\n") !== -1 || m.sdp.indexOf("\\r") !== -1,
        }
      : null;
    if (m.type && m.type !== "ice") {
      console.log("[in]", m.type, m.room, m.sender || ws.sender || "", sdpInfo || "");
    }

    // join room
    if (m.type === "join") {
      const room = m.room;
      if (!room) return;
      ws.room = room;
      if (m.sender) ws.sender = m.sender;
      if (m.viewerId) ws.viewerId = m.viewerId;

      const set = ensureRoom(room);
      set.add(ws);
      ws.send(JSON.stringify({ type: "joined", sender: ws.sender, room }));
      return;
    }

    // relay signaling
    if (!ws.room) return;
    const peers = rooms.get(ws.room) || new Set();
    const payload = {
      ...m,
      sender: m.sender || ws.sender,
      viewerId: m.viewerId || ws.viewerId,
      room: ws.room,
    };

    peers.forEach((p) => {
      if (p !== ws && p.readyState === WebSocket.OPEN) {
        if (m.type && m.type !== "ice") {
          console.log("[out]", m.type, ws.room, payload.sender || "", sdpInfo || "");
        }
        p.send(JSON.stringify(payload));
      }
    });
  });

  ws.on("close", () => {
    if (!ws.room) return;
    const set = rooms.get(ws.room);
    if (!set) return;
    set.delete(ws);
    if (set.size === 0) rooms.delete(ws.room);
  });
});

console.log("signaling on ws://0.0.0.0:8080");
