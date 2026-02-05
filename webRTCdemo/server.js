const fs = require("fs");
const https = require("https");
const WebSocket = require("ws");
const WebSocketServer = WebSocket.Server;

const keyPath = process.env.TLS_KEY;
const certPath = process.env.TLS_CERT;

if (!keyPath || !certPath) {
  console.error("Missing TLS_KEY/TLS_CERT env vars for WSS.");
  process.exit(1);
}

const tlsOptions = {
  key: fs.readFileSync(keyPath),
  cert: fs.readFileSync(certPath),
};

const server = https.createServer(tlsOptions, (req, res) => {
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end("ok");
});
const wss = new WebSocketServer({ server });

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

function roomHasOfferer(set) {
  for (const peer of set) {
    if (peer.role === "offerer") return true;
  }
  return false;
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
      console.log(
        "[in]",
        m.type,
        m.room,
        m.sender || ws.sender || "",
        sdpInfo || "",
      );
    }

    // join room
    if (m.type === "join") {
      const room = m.room;
      if (!room) return;
      ws.room = room;
      if (m.sender) ws.sender = m.sender;
      if (m.viewerId) ws.viewerId = m.viewerId;

      const set = ensureRoom(room);
      const joinIndex = set.size + 1;
      const hasOfferer = roomHasOfferer(set);
      ws.role = hasOfferer ? "answerer" : "offerer";
      set.add(ws);
      ws.send(
        JSON.stringify({
          type: "joined",
          sender: ws.sender,
          room,
          index: joinIndex,
          role: ws.role,
        }),
      );
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
          console.log(
            "[out]",
            m.type,
            ws.room,
            payload.sender || "",
            sdpInfo || "",
          );
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

server.listen(443, "0.0.0.0", () => {
  console.log("signaling on wss://0.0.0.0:443");
});
