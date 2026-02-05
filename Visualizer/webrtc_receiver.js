const WebSocket = require('websocket').client;
const { PeerConnection, RTCIceCandidate, RTCSessionDescription } = require('node-datachannel');

let pc = null;
let dc = null;
let ws = null;
let connection = null;

let isRemoteSet = false;
let pendingCandidates = [];
let currentConfig = null;
let remotePeerId = null;

// Callbacks
let onDataCallback = null;
let onStatusCallback = null;

function setOnData(callback) {
    onDataCallback = callback;
}

function setOnStatus(callback) {
    onStatusCallback = callback;
}

function updateStatus(status, details = "") {
    try {
        console.log(`[WebRTC] ${status} ${details}`);
        if (onStatusCallback) {
            onStatusCallback({ status, details, remotePeerId });
        }
    } catch (e) {
        console.error('[WebRTC Status Error]', e.message);
    }
}

function normalizeSDP(sdp) {
    // Strip NULL bytes
    let s = sdp.replace(/\0/g, '');

    // 2. Escape Resolution (JSON escape)
    s = s.replace(/\\r\\n/g, "\n").replace(/\\n/g, "\n").replace(/\\r/g, "\n");

    // 1. CRLF Enforcement (Convert all to \n first for normalization)
    s = s.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    s = s.replace(/\n+/g, "\n");

    // 3. Token Separation
    s = s.replace(/[ \t\u00a0]+([vosctamb])=/g, "\n$1=");

    // 4. Browser Cleanup
    s = s.replace(/^a=max-message-size:.*(?:\n)?/gm, "")
        .replace(/^a=extmap-allow-mixed.*(?:\n)?/gm, "")
        .replace(/^a=extmap:.*(?:\n)?/gm, "")
        .replace(/^a=rtcp-rsize.*(?:\n)?/gm, "");

    // 5. a=setup:actpass -> a=setup:active
    // We force active to trigger the connection immediately as a server
    s = s.replace(/^a=setup:actpass(?:\n)?$/gm, "a=setup:active\n");

    // 6. Candidate Sanitization
    s = s.replace(/^a=candidate:.*(?:\n)?/gm, "");

    // Final Normalization
    let lines = s.split('\n')
        .map(l => l.trim())
        .filter(l => l.length > 0);

    // Validation Check: Ensure v=0 is first
    if (lines.length > 0 && lines[0] !== 'v=0') {
        const vIndex = lines.findIndex(l => l.startsWith('v=0'));
        if (vIndex > -1) {
            const vLine = lines.splice(vIndex, 1)[0];
            lines.unshift(vLine);
        }
    }

    // 1. CRLF Enforcement (Final step: join with \r\n and ensure trailing \r\n)
    return lines.join('\r\n') + '\r\n';
}

function start(config) {
    // config: { signalingUrl, room, clientId }
    currentConfig = config;
    updateStatus('starting', `Room: ${config.room}`);
    connectSignaling();
}

function stop() {
    updateStatus('closing');
    if (connection) {
        try { connection.close(); } catch (e) { }
    }
    if (pc) {
        try { pc.close(); } catch (e) { }
    }
    pc = null;
    dc = null;
    isRemoteSet = false;
    pendingCandidates = [];
    connection = null;
    remotePeerId = null;
}

function connectSignaling() {
    // Ensure we start from a clean state
    if (pc || connection) {
        updateStatus('reconnecting', 'Closing previous session');
        stop();
    }

    ws = new WebSocket();

    ws.on('connect', (conn) => {
        updateStatus('signaling_connected');
        connection = conn;

        // 1. Join Room
        sendSignal({ type: "join", room: currentConfig.room });

        // 2. Proactively send 'ready' to announce presence to existing peers
        // This often triggers the Offerer to start the handshake.
        sendSignal({ type: "ready" });

        conn.on('message', (message) => {
            if (message.type === 'utf8') {
                handleSignal(JSON.parse(message.utf8Data));
            }
        });

        conn.on('close', () => {
            updateStatus('signaling_closed');
            // Auto-reconnect or status update can go here
        });

        conn.on('error', (err) => {
            updateStatus('error', `Signaling Error: ${err.message}`);
        });
    });

    ws.on('connectFailed', (error) => {
        updateStatus('error', `Connect Failed: ${error.toString()}`);
    });

    ws.connect(currentConfig.signalingUrl);
}

function sendSignal(payload) {
    if (connection && connection.connected) {
        let finalPayload = { sender: currentConfig.clientId, ...payload };
        if (payload.sdp) {
            finalPayload.sdp = normalizeSDP(payload.sdp);
        }
        connection.sendUTF(JSON.stringify(finalPayload));
    }
}

async function handleSignal(msg) {
    if (msg.sender === currentConfig.clientId) return;

    try {
        // Track remote peer ID
        if (!remotePeerId && msg.sender) {
            remotePeerId = msg.sender;
            updateStatus('peer_discovered', `Peer ID: ${remotePeerId}`);
        }

        if (msg.type === 'ready') {
            updateStatus('peer_ready', 'Peer joined room');
            // Reset if connection was already active to avoid stale state
            if (pc) {
                updateStatus('resetting', 'New peer ready signal');
                const oldConfig = currentConfig;
                stop();
                currentConfig = oldConfig;
            }
        } else if (msg.type === 'offer') {
            console.log("--- RAW REMOTE OFFER SDP ---");
            console.log(msg.sdp);
            console.log("----------------------------");

            const sdp = normalizeSDP(msg.sdp);
            console.log("--- NORMALIZED REMOTE OFFER SDP ---");
            console.log(sdp);
            console.log("----------------------------");

            updateStatus('offer_received');
            // If PC exists but we get a fresh offer, reset to handle clean state
            if (pc) {
                updateStatus('resetting', 'New offer received');
                const oldConfig = currentConfig;
                stop();
                currentConfig = oldConfig;
            }
            if (!pc) createPeerConnection();

            pc.setRemoteDescription(sdp, "offer");
            isRemoteSet = true;

            // Flush pending candidates
            if (pendingCandidates.length > 0) {
                pendingCandidates.forEach(cand => {
                    try { pc.addRemoteCandidate(cand.candidate, cand.sdpMid); } catch (e) { }
                });
                pendingCandidates = [];
            }
        } else if (msg.type === 'answer') {
            updateStatus('answer_received');
            if (pc) {
                const sdp = normalizeSDP(msg.sdp);
                pc.setRemoteDescription(sdp, "answer");
                isRemoteSet = true;
                // Flush pending candidates
                pendingCandidates.forEach(cand => {
                    try { pc.addRemoteCandidate(cand.candidate, cand.sdpMid); } catch (e) { }
                });
                pendingCandidates = [];
            }
        } else if (msg.type === 'ice') {
            if (pc && msg.candidate) {
                if (isRemoteSet) {
                    try { pc.addRemoteCandidate(msg.candidate.candidate, msg.candidate.sdpMid); } catch (e) { }
                } else {
                    pendingCandidates.push(msg.candidate);
                }
            }
        }
    } catch (e) {
        updateStatus('error', `Signaling error: ${e.message}`);
    }
}

function createPeerConnection() {
    updateStatus('creating_pc');
    pc = new PeerConnection("node-receiver", {
        iceServers: ["stun:stun.l.google.com:19302"]
    });

    pc.onLocalDescription((sdp, type) => {
        updateStatus('local_description_generated', type);
        console.log(`--- RAW LOCAL ${type.toUpperCase()} SDP ---`);
        console.log(sdp);

        const normalized = normalizeSDP(sdp);
        console.log(`--- NORMALIZED LOCAL ${type.toUpperCase()} SDP ---`);
        console.log(normalized);
        console.log("----------------------------");

        sendSignal({ type: type, sdp: sdp });
    });

    pc.onLocalCandidate((candidate, mid) => {
        const obj = { candidate: candidate, sdpMid: mid, sdpMLineIndex: 0 };
        sendSignal({ type: 'ice', candidate: obj });
    });

    pc.onDataChannel((channel) => {
        const label = (typeof channel.label === 'function') ? channel.label() : channel.label;
        updateStatus('datachannel_received', label || "unnamed");
        setupDataChannel(channel);
    });

    // STRICT ANSWERER: Do not create DataChannel proactively.
    // We will respond to the remote peer's DataChannel creation.
}

function setupDataChannel(channel) {
    dc = channel;
    dc.onOpen(() => {
        updateStatus('datachannel_open');
    });

    dc.onMessage((msg) => {
        if (Buffer.isBuffer(msg) || (msg instanceof Uint8Array)) {
            const buf = Buffer.isBuffer(msg) ? msg : Buffer.from(msg);
            if (buf.length >= 5) {
                const magic = buf.readUInt8(0);
                if (magic === 0xA7) { // Pong
                    const timestamp = buf.readUInt32LE(1);
                    const now = Date.now() & 0xFFFFFFFF;
                    const rtt = (now - timestamp + 0x100000000) % 0x100000000;
                    updateStatus('latency_update', `${rtt}ms`);
                    return;
                }
            }
        }
        if (onDataCallback) {
            onDataCallback(msg);
        }
    });

    dc.onClosed(() => {
        updateStatus('datachannel_closed');
    });
}

function send(data) {
    if (dc && isDataChannelOpen()) {
        try {
            if (Buffer.isBuffer(data)) {
                if (typeof dc.sendMessageBinary === 'function') {
                    dc.sendMessageBinary(data);
                } else {
                    dc.sendMessage(data);
                }
            } else {
                dc.sendMessage(data);
            }
            return true;
        } catch (e) {
            console.error('[WebRTC Send] Error:', e.message);
        }
    }
    return false;
}

function isConnected() {
    if (!pc) return false;
    const state = (typeof pc.state === 'function') ? pc.state() : pc.state;
    return state === 'connected';
}

function isDataChannelOpen() {
    if (!dc) return false;
    return (typeof dc.isOpen === 'function') ? dc.isOpen() : dc.isOpen;
}

module.exports = {
    start,
    stop,
    send,
    setOnData,
    setOnStatus,
    isConnected,
    isDataChannelOpen
};
