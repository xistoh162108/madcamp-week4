import Foundation
import Combine
import WebRTC

final class WebRTCClient: NSObject, ObservableObject {

    // UI 바인딩용
    @Published var status: String = "idle"
    @Published var dcState: String = "closed"
    @Published var localAnswerSdp: String = ""
    @Published var localIceJSONLines: [String] = []

    private let factory: RTCPeerConnectionFactory = RTCPeerConnectionFactory()
    private var peerConnection: RTCPeerConnection?
    private var dataChannel: RTCDataChannel?
    private var wsTask: URLSessionWebSocketTask?
    private var wsSession: URLSession?
    private var signalingRoom: String = "imu"
    private var signalingUrl: String = "wss://eokbba.shop"

    override init() {
        RTCInitializeSSL()
        super.init()
    }

    deinit {
        RTCCleanupSSL()
    }

    func startPeer() {
        status = "starting..."

        let config = RTCConfiguration()
        config.sdpSemantics = .unifiedPlan
        config.iceServers = [RTCIceServer(
            urlStrings: [
                "stun:3.37.140.87:3478",
                "turn:3.37.140.87:3478?transport=udp",
                "turn:3.37.140.87:3478?transport=tcp"
            ],
            username: "imuuser",
            credential: "imupass"
        )]

        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)

        let pc = factory.peerConnection(with: config, constraints: constraints, delegate: self)
        self.peerConnection = pc

        status = "peer ready (waiting datachannel from Chrome)"
    }

    func connectSignaling(urlString: String, room: String) {
        signalingUrl = urlString
        signalingRoom = room

        if peerConnection == nil {
            startPeer()
        }

        guard let url = URL(string: urlString) else {
            status = "signaling url invalid"
            return
        }

        wsTask?.cancel(with: .goingAway, reason: nil)

        let session = URLSession(configuration: .default)
        wsSession = session
        let task = session.webSocketTask(with: url)
        wsTask = task
        task.resume()

        status = "signaling connecting..."
        sendSignal(["type": "join", "room": signalingRoom])
        receiveLoop()
    }

    func disconnectSignaling() {
        wsTask?.cancel(with: .goingAway, reason: nil)
        wsTask = nil
        wsSession?.invalidateAndCancel()
        wsSession = nil
        DispatchQueue.main.async { self.status = "signaling disconnected" }
    }

    private func receiveLoop() {
        wsTask?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let error):
                DispatchQueue.main.async {
                    self.status = "signaling recv error: \(error.localizedDescription)"
                }
            case .success(let message):
                switch message {
                case .string(let text):
                    self.handleSignal(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        self.handleSignal(text)
                    }
                @unknown default:
                    break
                }
            }
            self.receiveLoop()
        }
    }

    private func handleSignal(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        guard let type = obj["type"] as? String else { return }

        if type == "offer", let sdp = obj["sdp"] as? String {
            DispatchQueue.main.async { self.status = "offer received" }
            setRemoteOfferSdp(sdp)
            createAnswerAndSend()
            return
        }

        if type == "joined" {
            DispatchQueue.main.async { self.status = "signaling joined (\(self.signalingRoom))" }
            sendSignal(["type": "ready"])
            return
        }

        if type == "full" {
            DispatchQueue.main.async { self.status = "signaling room full" }
            return
        }

        if type == "ice", let cand = obj["candidate"] as? [String: Any] {
            addRemoteIceCandidate(cand)
            return
        }
    }

    private func sendSignal(_ payload: [String: Any]) {
        guard let wsTask = wsTask else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else { return }
        if let text = String(data: data, encoding: .utf8) {
            wsTask.send(.string(text)) { _ in }
        }
    }

    // ✅ Chrome에서 만든 Offer를 iPhone에 적용
    func setRemoteOfferSdp(_ sdpString: String) {
        guard let pc = peerConnection else {
            status = "peerConnection nil (Start Peer 먼저)"
            return
        }

        var sdp = sdpString
            .trimmingCharacters(in: .whitespacesAndNewlines)
            // Normalize line endings to CRLF for WebRTC parsing
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\n", with: "\r\n")
        if !sdp.hasSuffix("\r\n") {
            sdp.append("\r\n")
        }
        if !sdp.contains("\r\n") {
            status = "offer SDP line breaks missing (줄바꿈 확인)"
            return
        }
        guard !sdp.isEmpty else {
            status = "offer SDP empty (붙여넣기 확인: v=0 로 시작해야 함)"
            return
        }
        guard sdp.contains("v=0") else {
            status = "not SDP (첫 줄이 v=0 인지 확인)"
            return
        }
        
        
        print("OFFER len=", sdp.count)
        print("OFFER head=\n", sdp.prefix(80))
        print("OFFER tail=\n", sdp.suffix(80))


        status = "setting remote offer..."
        let desc = RTCSessionDescription(type: .offer, sdp: sdp)

        pc.setRemoteDescription(desc) { [weak self] err in
            DispatchQueue.main.async {
                if let err = err {
                    self?.status = "setRemote offer err: \(err.localizedDescription)"
                } else {
                    self?.status = "remote offer set"
                }
            }
        }
    }

    // ✅ iPhone이 Answer 생성해서 Chrome에 전달
    func createAnswer() {
        guard let pc = peerConnection else {
            status = "peerConnection is nil (Start Peer 먼저)"
            return
        }
        status = "creating answer..."

        let constraints = RTCMediaConstraints(
            mandatoryConstraints: [
                "OfferToReceiveAudio": "false",
                "OfferToReceiveVideo": "false"
            ],
            optionalConstraints: nil
        )

        pc.answer(for: constraints) { [weak self] (sdp: RTCSessionDescription?, error: Error?) in
            guard let self = self else { return }

            if let error = error {
                DispatchQueue.main.async { self.status = "answer error: \(error.localizedDescription)" }
                return
            }
            guard let sdp = sdp else {
                DispatchQueue.main.async { self.status = "answer error: nil sdp" }
                return
            }

            pc.setLocalDescription(sdp) { err in
                DispatchQueue.main.async {
                    if let err = err {
                        self.status = "setLocal answer err: \(err.localizedDescription)"
                    } else {
                        self.localAnswerSdp = sdp.sdp
                        self.status = "answer ready (copy to Chrome)"
                    }
                }
            }
        }
    }

    func createAnswerAndSend() {
        guard let pc = peerConnection else { return }
        status = "creating answer..."

        let constraints = RTCMediaConstraints(
            mandatoryConstraints: [
                "OfferToReceiveAudio": "false",
                "OfferToReceiveVideo": "false"
            ],
            optionalConstraints: nil
        )

        pc.answer(for: constraints) { [weak self] (sdp: RTCSessionDescription?, error: Error?) in
            guard let self = self else { return }

            if let error = error {
                DispatchQueue.main.async { self.status = "answer error: \(error.localizedDescription)" }
                return
            }
            guard let sdp = sdp else {
                DispatchQueue.main.async { self.status = "answer error: nil sdp" }
                return
            }

            pc.setLocalDescription(sdp) { err in
                DispatchQueue.main.async {
                    if let err = err {
                        self.status = "setLocal answer err: \(err.localizedDescription)"
                    } else {
                        self.localAnswerSdp = sdp.sdp
                        self.status = "answer sent"
                        self.sendSignal(["type": "answer", "sdp": sdp.sdp])
                    }
                }
            }
        }
    }

    // ✅ Chrome에서 온 ICE(JSON 한 줄) 추가
    func addRemoteIceJSONLine(_ jsonLine: String) {
        guard let pc = peerConnection else { return }
        guard let data = jsonLine.data(using: .utf8) else { return }

        do {
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard
                let candidate = obj?["candidate"] as? String,
                let mline = obj?["sdpMLineIndex"] as? Int
            else { return }

            let mid = obj?["sdpMid"] as? String
            let ice = RTCIceCandidate(sdp: candidate, sdpMLineIndex: Int32(mline), sdpMid: mid)
            pc.add(ice)
        } catch {
            status = "remote ICE parse error"
        }
    }

    private func addRemoteIceCandidate(_ obj: [String: Any]) {
        guard let pc = peerConnection else { return }
        guard
            let candidate = obj["candidate"] as? String,
            let mline = obj["sdpMLineIndex"] as? Int
        else { return }
        let mid = obj["sdpMid"] as? String
        let ice = RTCIceCandidate(sdp: candidate, sdpMLineIndex: Int32(mline), sdpMid: mid)
        pc.add(ice)
    }

    // ✅ MotionStreamer에서 바이너리 패킷 전송
    func sendBinary(_ data: Data) {
        guard let dc = dataChannel, dc.readyState == .open else { return }
        
        // 디버그: 가끔만
        if Int.random(in: 0..<50) == 0 {
            print("sendBinary bytes=", data.count)
        }

        let buf = RTCDataBuffer(data: data, isBinary: true)
        _ = dc.sendData(buf)
    }

    func closeDataChannel() {
        dataChannel?.close()
        dataChannel = nil
        dcState = "closed"
    }
}

// MARK: - RTCPeerConnectionDelegate
extension WebRTCClient: RTCPeerConnectionDelegate {
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}

    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}

    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
        DispatchQueue.main.async { self.status = "ICE conn: \(newState.rawValue)" }
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {
        DispatchQueue.main.async { self.status = "ICE gathering: \(newState.rawValue)" }
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {
        let obj: [String: Any] = [
            "candidate": candidate.sdp,
            "sdpMLineIndex": Int(candidate.sdpMLineIndex),
            "sdpMid": candidate.sdpMid ?? "0"
        ]
        if let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
           let line = String(data: data, encoding: .utf8) {
            DispatchQueue.main.async { self.localIceJSONLines.append(line) }
        }
        sendSignal(["type": "ice", "candidate": obj])
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}

    // ✅ Chrome이 DataChannel을 만들면 여기로 들어옴
    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {
        self.dataChannel = dataChannel
        dataChannel.delegate = self
        DispatchQueue.main.async {
            self.status = "datachannel received: \(dataChannel.label)"
        }
    }
}

// MARK: - RTCDataChannelDelegate
extension WebRTCClient: RTCDataChannelDelegate {
    func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        let s: String
        switch dataChannel.readyState {
        case .connecting: s = "connecting"
        case .open: s = "open"
        case .closing: s = "closing"
        case .closed: s = "closed"
        @unknown default: s = "unknown"
        }
        DispatchQueue.main.async { self.dcState = s }
    }

    func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        // sender라서 안 씀
    }
}
