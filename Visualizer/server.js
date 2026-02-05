const dgram = require('dgram');
const express = require('express');
const app = express();
const http = require('http');
const server = http.createServer(app);
const io = require('socket.io')(server);
const path = require('path');

const UDP_PORT = 5002;
const HTTP_PORT = 3000;
const { spawn } = require('child_process');
const webrtc = require('./webrtc_receiver');

// UDP Server (Receives from Vision Pro)
const udpServer = dgram.createSocket('udp4');

const ML_SERVER_PORT = 5001;
const ML_RESULT_PORT = 5003;

let isMlProcessing = false;
const mlResultSocket = dgram.createSocket('udp4');

mlResultSocket.on('message', (msg) => {
  try {
    const result = JSON.parse(msg.toString());
    console.log(`[ML Result] Received data from ML Server (${msg.length} bytes)`);
    console.log(`[IO] Emitting ml_data to frontend`);
    io.emit('ml_data', result);
    isMlProcessing = false;
  } catch (e) {
    console.error('[ML Result] Parse Error:', e.message);
  }
});

mlResultSocket.bind(ML_RESULT_PORT, '127.0.0.1');

mlResultSocket.on('listening', () => {
  const addr = mlResultSocket.address();
  console.log(`[ML Result] Listener active on ${addr.address}:${addr.port}`);
});

mlResultSocket.on('error', (err) => {
  console.error(`[ML Result] Socket Error: ${err.message}`);
});

let forwardCount = 0;
function forwardToMLServer(data) {
  if (isMlProcessing) return;

  isMlProcessing = true;
  const payload = Buffer.from(JSON.stringify(data));

  udpServer.send(payload, ML_SERVER_PORT, '127.0.0.1', (err) => {
    if (err) {
      isMlProcessing = false;
      console.error(`[ML Forward] UDP Error: ${err.message}`);
    } else {
      forwardCount++;
      if (forwardCount === 1) console.log(`[ML Forward] SUCCESS: Sent first packet to ML Server on port ${ML_SERVER_PORT}`);
      if (forwardCount % 100 === 0) console.log(`[ML Forward] Status: Total forwarded packets = ${forwardCount}`);
    }
  });

  // Safety timeout
  setTimeout(() => {
    if (isMlProcessing) isMlProcessing = false;
  }, 200);
}

// --- WebRTC Logic ---
webrtc.setOnStatus((evt) => {
  console.log(`[WebRTC Update] ${evt.status}: ${evt.details}`);
  io.emit('webrtc_status', evt);
});

webrtc.setOnData((msg) => {
  try {
    const isBinary = Buffer.isBuffer(msg);
    let rawPreview = "";
    let hex = "";

    if (isBinary) {
      rawPreview = msg.toString('utf8').substring(0, 100);
      hex = msg.toString('hex').match(/.{1,2}/g).join(' ');
    } else {
      rawPreview = String(msg);
    }

    console.log(`[WebRTC Incoming] Received ${msg.length} bytes`);

    // Emit to frontend dashboard
    io.emit('webrtc_data', {
      timestamp: Date.now(),
      length: msg.length,
      raw: rawPreview,
      hex: hex,
      isBinary: isBinary
    });
  } catch (e) {
    console.error('[WebRTC Data Error]:', e.message);
  }
});

let isWebRTCStreaming = true;

function serializeVPData(data) {
  // 1. Header (6 bytes)
  // Magic (1B), Flags (1B), Timestamp (4B)
  let flags = 0;
  if (data.head) flags |= (1 << 0);
  if (data.leftHand) flags |= (1 << 1);
  if (data.rightHand) flags |= (1 << 2);
  if (data.controller) flags |= (1 << 3);
  if (data.connection.calibrating) flags |= (1 << 6);
  if (data.connection.calibrated) flags |= (1 << 7);

  // Calculate size
  let size = 6;
  if (data.head) size += 28;
  if (data.leftHand) size += 1 + (data.leftHand.joints.length * 12) + 12;
  if (data.rightHand) size += 1 + (data.rightHand.joints.length * 12) + 12;
  if (data.controller) size += 28;

  const buf = Buffer.alloc(size);
  let off = 0;

  // Header
  buf.writeUInt8(0xA5, off++);
  buf.writeUInt8(flags, off++);
  buf.writeUInt32LE(data.timestamp, off); off += 4;

  // 1. Head
  if (data.head) {
    data.head.pos.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    data.head.rot.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
  }

  // 2. Left Hand
  if (data.leftHand) {
    buf.writeUInt8(data.leftHand.joints.length, off++);
    data.leftHand.joints.forEach(j => {
      j.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    });
    data.leftHand.palmPose.pos.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    data.leftHand.palmPose.rot.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
  }

  // 3. Right Hand
  if (data.rightHand) {
    buf.writeUInt8(data.rightHand.joints.length, off++);
    data.rightHand.joints.forEach(j => {
      j.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    });
    data.rightHand.palmPose.pos.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    data.rightHand.palmPose.rot.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
  }

  // 4. Controller
  if (data.controller) {
    data.controller.pos.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
    data.controller.rot.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
  }

  return buf;
}

let seq72 = 0;
let lastVpData = null;
let lastVpTimestamp = 0;

/**
 * Calculates joint angles (Skeleton) from raw VP tracking data.
 * Derives body yaw, head pitch, and arm angles using geometric IK.
 */
function calculateJointAngles(data) {
  const angles = {
    bodyYaw: 0,
    headPitch: 0,
    left: { armPitch: 0, armYaw: 0, armAbd: 0, elbowPitch: 0 },
    right: { armPitch: 0, armYaw: 0, armAbd: 0, elbowPitch: 0 }
  };

  if (!data || !data.head) return angles;

  // 1. Head angles (Derived from Quat)
  const [qx, qy, qz, qw] = data.head.rot;
  angles.bodyYaw = Math.atan2(2 * (qw * qy + qx * qz), 1 - 2 * (qy * qy + qz * qz));
  angles.headPitch = Math.asin(Math.max(-1, Math.min(1, 2 * (qw * qx - qy * qz))));

  const headPos = data.head.pos;

  const processArm = (hand, side) => {
    // ARKit standard hand joints: Index 0=Wrist, Index 26=Forearm/Elbow region
    if (!hand || !hand.joints || hand.joints.length < 27) return;

    // Estimate Shoulder position relative to head
    const sideSign = side === 'left' ? -1 : 1;
    const shoulderPos = [
      headPos[0] + (sideSign * 0.15),
      headPos[1] - 0.25,
      headPos[2] - 0.05
    ];

    const elbowPos = hand.joints[26];
    const wristPos = hand.joints[0];

    // Upper Arm Vector (Shoulder -> Elbow)
    const vU = [elbowPos[0] - shoulderPos[0], elbowPos[1] - shoulderPos[1], elbowPos[2] - shoulderPos[2]];
    const lenU = Math.sqrt(vU[0] ** 2 + vU[1] ** 2 + vU[2] ** 2);

    // Lower Arm Vector (Elbow -> Wrist)
    const vL = [wristPos[0] - elbowPos[0], wristPos[1] - elbowPos[1], wristPos[2] - elbowPos[2]];
    const lenL = Math.sqrt(vL[0] ** 2 + vL[1] ** 2 + vL[2] ** 2);

    if (lenU > 0.01 && lenL > 0.01) {
      // 1. Elbow Pitch
      const dot = (vU[0] * vL[0] + vU[1] * vL[1] + vU[2] * vL[2]) / (lenU * lenL);
      angles[side].elbowPitch = Math.acos(Math.max(-1, Math.min(1, dot)));

      // 2. Arm Pitch/Yaw
      angles[side].armPitch = Math.asin(-vU[1] / lenU);
      angles[side].armYaw = Math.atan2(vU[0], -vU[2]);
    }
  };

  processArm(data.leftHand, 'left');
  processArm(data.rightHand, 'right');

  return angles;
}

/**
 * Packs tracking data into a fixed 72-byte binary format.
 * If data is null, generates a "Base Pose" (Neutral).
 */
function serialize72BytePacket(data) {
  const isStale = !data || (Date.now() - lastVpTimestamp > 1000);
  const angles = isStale ? {
    bodyYaw: 0, headPitch: 0,
    left: { armPitch: -0.2, armYaw: -0.5, armAbd: 0, elbowPitch: 0.1 },
    right: { armPitch: -0.2, armYaw: 0.5, armAbd: 0, elbowPitch: 0.1 }
  } : calculateJointAngles(data);

  const buf = Buffer.alloc(72);
  let off = 0;

  // 1. Header (4B) - Use 31st bit of seq as "Stale" flag
  let finalSeq = seq72++;
  if (isStale) finalSeq |= 0x80000000;
  buf.writeUInt32LE(finalSeq >>> 0, off); off += 4;

  // 2. Root Pose (px, py, pz, qx, qy, qz, qw)
  const headPos = (!isStale && data.head) ? data.head.pos : [0, 1.6, 0]; // 1.6m is avg eye height
  const headRot = (!isStale && data.head) ? data.head.rot : [0, 0, 0, 1];
  headPos.forEach(v => { buf.writeFloatLE(v, off); off += 4; });
  headRot.forEach(v => { buf.writeFloatLE(v, off); off += 4; });

  // 3. Body/Head Angles (8B)
  buf.writeFloatLE(angles.bodyYaw, off); off += 4;
  buf.writeFloatLE(angles.headPitch, off); off += 4;

  // 4. Left Arm (16B)
  buf.writeFloatLE(angles.left.armPitch, off); off += 4;
  buf.writeFloatLE(angles.left.armYaw, off); off += 4;
  buf.writeFloatLE(angles.left.armAbd, off); off += 4;
  buf.writeFloatLE(angles.left.elbowPitch, off); off += 4;

  // 5. Right Arm (16B)
  buf.writeFloatLE(angles.right.armPitch, off); off += 4;
  buf.writeFloatLE(angles.right.armYaw, off); off += 4;
  buf.writeFloatLE(angles.right.armAbd, off); off += 4;
  buf.writeFloatLE(angles.right.elbowPitch, off); off += 4;

  return buf;
}

// --- Vision Pro Auto-Discovery (Bonjour) ---
const discoveredDevices = new Map(); // Name -> { ip, lastSeen }
let activeVpIP = null;
let activeVpTimeout = null;

function updateActiveVP(ip) {
  if (activeVpIP === null) {
    io.emit('log', `System: Data stream active from ${ip}`);
  }
  activeVpIP = ip;
  clearTimeout(activeVpTimeout);
  activeVpTimeout = setTimeout(() => {
    io.emit('log', `System: Data stream timed out from ${activeVpIP}`);
    activeVpIP = null;
    io.emit('bonjour_devices', { devices: Array.from(discoveredDevices.entries()), activeIP: activeVpIP });
  }, 3000); // Clear after 3s of silence

  io.emit('bonjour_devices', { devices: Array.from(discoveredDevices.entries()), activeIP: activeVpIP });
}

function startVPDiscovery() {
  console.log("Discovery: Searching for Vision Pro (_madcamp-stream._udp)...");

  // 1. Browse for the service
  const browser = spawn('dns-sd', ['-B', '_madcamp-stream._udp']);

  browser.stdout.on('data', (data) => {
    const lines = data.toString().split('\n');
    for (let line of lines) {
      if (line.trim()) console.log(`Discovery (Raw): ${line.trim()}`);

      if (line.includes('Add') && line.includes('_madcamp-stream._udp')) {
        const parts = line.split(/\s+/).filter(p => p.length > 0);
        const serviceName = parts[parts.length - 1];

        if (serviceName && serviceName !== 'local.') {
          console.log(`Discovery: Found service "${serviceName}"! Resolving...`);
          io.emit('log', `Discovery: Found service "${serviceName}"`);
          resolveVP(serviceName);
        }
      }
    }
  });

  browser.stderr.on('data', (data) => {
    console.error(`Discovery Browser Error: ${data.toString()}`);
  });

  function resolveVP(name) {
    console.log(`Discovery: Resolving instance "${name}"...`);

    // 1. Resolve Instance Name -> Hostname
    // Output format: "... can be reached at VisionPro.local.:5002 ..."
    const lookup = spawn('dns-sd', ['-L', name, '_madcamp-stream._udp', 'local']);

    lookup.stdout.on('data', (data) => {
      const line = data.toString();
      console.log(`Discovery Lookup: ${line.trim()}`);

      // Match "can be reached at <hostname>."
      const match = line.match(/can be reached at\s+([^\s:]+)/);
      if (match) {
        let hostname = match[1];
        // Remove trailing dot if present
        if (hostname.endsWith('.')) hostname = hostname.slice(0, -1);

        console.log(`Discovery: "${name}" is at hostname "${hostname}". Resolving IP...`);
        io.emit('log', `Discovery: "${name}" resolved to hostname ${hostname}`);
        resolveIP(hostname, name);
        lookup.kill();
      }
    });

    lookup.stderr.on('data', (data) => {
      console.error(`Discovery Lookup Error: ${data.toString()}`);
    });
  }

  function resolveIP(hostname, originalName) {
    // 2. Resolve Hostname -> IP
    const resolver = spawn('dns-sd', ['-G', 'v4', hostname]);

    resolver.stdout.on('data', (data) => {
      const line = data.toString();
      console.log(`Discovery Resolver: ${line.trim()}`);

      // Look for the IPv4 pattern
      const match = line.match(/\d+\.\d+\.\d+\.\d+/);
      if (match) {
        const ip = match[0];
        console.log(`Discovery: Resolved "${originalName}" (${hostname}) at ${ip}. Sending Handshake...`);
        io.emit('log', `Discovery: Resolved ${hostname} to IP ${ip}`);

        // Update Device List
        discoveredDevices.set(originalName, { ip: ip, lastSeen: Date.now() });
        io.emit('bonjour_devices', { devices: Array.from(discoveredDevices.entries()), activeIP: activeVpIP });

        // Auto-handshake for our default target
        if (originalName === 'VisionPro-Data') {
          sendHandshake(ip);
        }
        resolver.kill();
      }
    });

    resolver.stderr.on('data', (data) => {
      console.error(`Discovery Resolver Error: ${data.toString()}`);
    });
  }

  function sendHandshake(ip) {
    // Send 0xBB Handshake to initiate streaming
    const packet = Buffer.alloc(1);
    packet.writeUInt8(0xBB, 0);
    udpServer.send(packet, UDP_PORT, ip, (err) => {
      if (err) {
        console.error("Discovery: Handshake failed:", err);
        io.emit('log', `Error: Handshake to ${ip} failed`);
      } else {
        console.log(`Discovery: Handshake sent to ${ip}`);
        io.emit('log', `System: Handshake sent to ${ip}`);
      }
    });
  }
}

// Socket Connection Handler to send initial list
io.on('connection', (socket) => {
  socket.emit('bonjour_devices', { devices: Array.from(discoveredDevices.entries()), activeIP: activeVpIP });

  socket.on('manual_connect', (ip) => {
    const packet = Buffer.alloc(1);
    packet.writeUInt8(0xBB, 0);
    udpServer.send(packet, UDP_PORT, ip);
    console.log(`Discovery: Manual handshake requested for ${ip}`);
    io.emit('log', `System: Manual handshake requested for ${ip}`);
  });

  socket.on('webrtc_connect', (data) => {
    // data: { url, room }
    const clientId = "node-server-" + Math.floor(Math.random() * 100000);
    webrtc.start({
      signalingUrl: data.url,
      room: data.room,
      clientId: clientId
    });
  });

  socket.on('webrtc_disconnect', () => {
    webrtc.stop();
  });

  socket.on('webrtc_stream_toggle', (enabled) => {
    isWebRTCStreaming = enabled;
    console.log(`[WebRTC] Streaming ${enabled ? 'ENABLED' : 'DISABLED'}`);
  });
});

// Periodic Pings & Test Pulse
// WebRTC Skeleton Stream Loop (30Hz)
// Ensures the remote peer always has a pose, even if VP is disconnected
setInterval(() => {
  if (isWebRTCStreaming && webrtc.isConnected() && webrtc.isDataChannelOpen()) {
    const isStale = (Date.now() - lastVpTimestamp > 1000);
    if (isStale) {
      // Send Base Pose if VP data is missing
      const poseBuf = serialize72BytePacket(null);
      webrtc.send(poseBuf);
    }
  }
}, 33); // ~30Hz

setInterval(() => {
  if (webrtc.isConnected() && webrtc.isDataChannelOpen()) {
    // 1. RTT Ping (Binary)
    const ping = Buffer.alloc(5);
    ping.writeUInt8(0xA6, 0);
    ping.writeUInt32LE(Date.now() & 0xFFFFFFFF, 1);
    webrtc.send(ping);

    // 2. Test Pulse (String)
    const testMsg = "Hello World!";
    if (webrtc.send(testMsg)) {
      console.log(`[WebRTC Outgoing] Sent: ${testMsg}`);
    }
  }
}, 1000);

startVPDiscovery();

udpServer.on('error', (err) => {
  console.log(`UDP Server error:\n${err.stack}`);
  udpServer.close();
});

let lastVpEndpoint = null;
let currentVpMacLatency = 0;

udpServer.on('message', (msg, rinfo) => {
  lastVpEndpoint = rinfo;
  try {
    if (msg.length < 1) return;
    const magic = msg.readUInt8(0);

    // Always update active IP for any valid packet from VP
    updateActiveVP(rinfo.address);

    // 1. Log Packet 'L' (0x4C)
    if (magic === 0x4C) {
      if (msg.length < 3) return;
      const length = msg.readUInt16LE(1);
      if (msg.length < 3 + length) return;
      const logMsg = msg.toString('utf8', 3, 3 + length);
      console.log(`[VP Log] ${logMsg}`);
      io.emit('log', `[VP] ${logMsg}`);
      return;
    }

    // 2. Pong Packet 'C' (0xCC)
    if (magic === 0xCC) {
      if (msg.length < 5) return;
      const originalTs = msg.readUInt32LE(1);
      const nowMs = Date.now() % 0xFFFFFFFF;
      const rtt = nowMs - originalTs;
      currentVpMacLatency = rtt > 0 ? rtt / 2 : 0;
      return;
    }

    // 3. Data Packet 'P' (0x50)
    if (magic !== 0x50) {
      if (magic !== 0xBB) { // Ignore our own pings if echoed
        // console.log(`UDP Unknown: 0x${magic.toString(16).toUpperCase()} (${msg.length} bytes) from ${rinfo.address}`);
      }
      return;
    }

    // If we reached here, it's a 0x50 Data Packet
    if (msg.length < 14) return;

    if (forwardCount % 100 === 0) {
      console.log(`[UDP] Received Data Packet 'P' (${msg.length} bytes) from ${rinfo.address}`);
    }

    const flags = msg.readUInt8(1);
    const timestamp = msg.readUInt32LE(2); // VP Send Time (ms)
    const latencyIpVp = msg.readFloatLE(6); // IP -> VP Latency (ms)
    const calibProgress = msg.readFloatLE(10); // Calibration Progress (0.0 - 1.0)

    // Parse Flags
    const isControllerConnected = (flags & (1 << 4)) !== 0;
    const isCalibrating = (flags & (1 << 5)) !== 0; // Bit 5
    const isCalibrated = (flags & (1 << 6)) !== 0; // Bit 6

    // 🔹 Added state transition logging
    if (lastVpData) {
      if (!lastVpData.connection.calibrating && isCalibrating) {
        console.log(">>> [STATE] Calibration STARTED (Sampling 0.4s)");
        io.emit('log', "System: Calibration STARTED");
      }
      if (lastVpData.connection.calibrating && !isCalibrating) {
        console.log("<<< [STATE] Calibration STOPPED/FINALIZED");
        io.emit('log', "System: Calibration STOPPED");
      }
      if (!lastVpData.connection.calibrated && isCalibrated) {
        console.log("✅ [STATE] System CALIBRATED");
        io.emit('log', "System: CALIBRATED SUCCESS");
      }
    }

    const latencyVpMac = currentVpMacLatency; // Use measured RTT/2
    const totalLatency = latencyIpVp + latencyVpMac;

    let offset = 14;

    const data = {
      timestamp: timestamp,
      latency: {
        ip_vp: latencyIpVp,
        vp_mac: latencyVpMac,
        total: totalLatency
      },
      connection: {
        controller: isControllerConnected,
        calibrating: isCalibrating,
        progress: calibProgress,
        calibrated: isCalibrated
      },
      head: null,
      leftHand: null,
      rightHand: null,
      controller: null
    };

    // Helper: Read Transform (Pos 12 + Rot 16 = 28 bytes)
    function readTransform() {
      const px = msg.readFloatLE(offset); offset += 4;
      const py = msg.readFloatLE(offset); offset += 4;
      const pz = msg.readFloatLE(offset); offset += 4;

      const qx = msg.readFloatLE(offset); offset += 4;
      const qy = msg.readFloatLE(offset); offset += 4;
      const qz = msg.readFloatLE(offset); offset += 4;
      const qw = msg.readFloatLE(offset); offset += 4;

      return { pos: [px, py, pz], rot: [qx, qy, qz, qw] };
    }

    // Helper: Read Skeleton
    function readSkeleton() {
      const count = msg.readUInt8(offset); offset += 1;
      const joints = [];
      for (let i = 0; i < count; i++) {
        const px = msg.readFloatLE(offset); offset += 4;
        const py = msg.readFloatLE(offset); offset += 4;
        const pz = msg.readFloatLE(offset); offset += 4;
        joints.push([px, py, pz]);
      }

      // Read Palm Pose (28 bytes: 12 pos + 16 rot)
      const px = msg.readFloatLE(offset); offset += 4;
      const py = msg.readFloatLE(offset); offset += 4;
      const pz = msg.readFloatLE(offset); offset += 4;

      const qx = msg.readFloatLE(offset); offset += 4;
      const qy = msg.readFloatLE(offset); offset += 4;
      const qz = msg.readFloatLE(offset); offset += 4;
      const qw = msg.readFloatLE(offset); offset += 4;

      return { joints: joints, palmPose: { pos: [px, py, pz], rot: [qx, qy, qz, qw] } };
    }

    // 1. Head (Flag bit 0)
    if (flags & (1 << 0)) {
      data.head = readTransform();
    }

    // 2. Left Hand (Flag bit 1)
    if (flags & (1 << 1)) {
      data.leftHand = readSkeleton();
    }

    // 3. Right Hand (Flag bit 2)
    if (flags & (1 << 2)) {
      data.rightHand = readSkeleton();
    }

    // 4. Controller (Flag bit 3)
    if (flags & (1 << 3)) {
      data.controller = readTransform();
    }

    // 4. Update Global State
    lastVpData = data;
    lastVpTimestamp = Date.now();

    // 5. Broadcast via WebRTC
    if (isWebRTCStreaming && webrtc.isConnected() && webrtc.isDataChannelOpen()) {
      // Send 72-byte Skeleton Pose (Optimized for avatar control)
      const poseBuf = serialize72BytePacket(data);
      webrtc.send(poseBuf);
    }

    forwardCount++;
    // Forward to Frontend
    if (forwardCount % 100 === 0) console.log(`[IO] Emitting vp_data to frontend`);
    io.emit('vp_data', data);

    // Forward to ML Server
    forwardToMLServer(data);

  } catch (e) {
    console.error("Parse error:", e.message);
  }
});

udpServer.on('listening', () => {
  const address = udpServer.address();
  console.log(`UDP Server listening on ${address.address}:${address.port}`);
});

udpServer.bind(UDP_PORT);

// Periodically Ping Vision Pro & Log Status
setInterval(() => {
  if (lastVpEndpoint) {
    const ping = Buffer.alloc(5);
    ping.writeUInt8(0xBB, 0);
    ping.writeUInt32LE(Date.now() % 0xFFFFFFFF, 1);
    udpServer.send(ping, lastVpEndpoint.port, lastVpEndpoint.address);
  }
}, 1000);

setInterval(() => {
  if (activeVpIP) {
    io.emit('log', `Status: Connected to ${activeVpIP} | Latency: ${currentVpMacLatency.toFixed(1)}ms`);
  } else {
    io.emit('log', `Status: Waiting for Vision Pro data...`);
  }
}, 5000);

// HTTP Server
app.use(express.static(path.join(__dirname, 'public')));

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public/index.html'));
});

server.listen(HTTP_PORT, () => {
  console.log(`Visualizer running at http://localhost:${HTTP_PORT}`);
});
