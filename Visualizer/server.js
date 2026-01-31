const dgram = require('dgram');
const express = require('express');
const app = express();
const http = require('http').createServer(app);
const io = require('socket.io')(http);
const path = require('path');

const UDP_PORT = 8080;
const HTTP_PORT = 3000;

// UDP Server (Receives from Vision Pro)
const udpServer = dgram.createSocket('udp4');

udpServer.on('error', (err) => {
  console.log(`UDP Server error:\n${err.stack}`);
  udpServer.close();
});

// Buffer for ML
const frameBuffer = [];
const ML_SERVER_URL = 'http://127.0.0.1:5001/predict';

udpServer.on('message', (msg, rinfo) => {
  try {
    const data = JSON.parse(msg.toString());

    // Debug: Log first packet to check timestamp
    if (!this.hasLogged) {
      console.log("First packet:", data);
      this.hasLogged = true;
    }

    // 1. Direct Forward
    io.emit('pose_update', data);

    // 2. Buffer for ML (30Hz Logic)
    const now = Date.now();
    if (!this.lastMlSend || now - this.lastMlSend > 33) {
      this.lastMlSend = now;
      sendToML(data);
    }

  } catch (e) {
    console.error("JSON parse error:", e.message);
  }
});

async function sendToML(frameData) {
  try {
    const response = await fetch(ML_SERVER_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(frameData)
    });

    if (response.ok) {
      const fullSkeleton = await response.json();
      // Emit full skeleton
      io.emit('full_skeleton', fullSkeleton);
    } else {
      console.error("ML Server error:", response.status, response.statusText);
    }
  } catch (e) {
    console.error("ML Server connection error:", e.message);
  }
}


udpServer.on('listening', () => {
  const address = udpServer.address();
  console.log(`UDP Server listening on ${address.address}:${address.port}`);
});

udpServer.bind(UDP_PORT);

// HTTP Server (For Frontend)
app.use(express.static(path.join(__dirname, 'public')));

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public/index.html'));
});

// Middleware for JSON body
app.use(express.json());

// Proxy: Calibration Start
app.post('/calibrate/start', async (req, res) => {
  try {
    const response = await fetch('http://127.0.0.1:5001/calibrate/start', { method: 'POST' });
    const data = await response.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Proxy: Calibration Finish
app.post('/calibrate/finish', async (req, res) => {
  try {
    const response = await fetch('http://127.0.0.1:5001/calibrate/finish', { method: 'POST' });
    const data = await response.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

http.listen(HTTP_PORT, () => {
  console.log(`Web Server listening on http://localhost:${HTTP_PORT}`);
  console.log(`Open your browser to http://localhost:${HTTP_PORT}`);
});
