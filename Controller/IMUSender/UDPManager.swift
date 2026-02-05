//
//  UDPManager.swift
//  IMUSender
//
//  Created by Auto-Agent on 2/2/26.
//

import Foundation
import Network
import Combine

final class UDPManager: ObservableObject {
    // Publisher for received data
    var dataReceived = PassthroughSubject<(Data, NWEndpoint), Never>()
    
    // Connection State
    @Published var isReady: Bool = false
    @Published var listeningPort: UInt16?
    @Published var isConnectedToVisionPro: Bool = false
    @Published var visionProIP: String?
    
    enum SwordMode { case HELD, DROPPED }
    @Published var swordMode: SwordMode = .HELD
    
    // Callbacks for UI
    var onCalibrationSuccess: (() -> Void)?
    var onCalibrationAbort: ((String) -> Void)?
    
    private var listener: NWListener?
    private var connection: NWConnection?
    private let queue = DispatchQueue(label: "com.madcamp.udp.queue")
    private var cancellables = Set<AnyCancellable>()
    
    // Constants (Document V2)
    private let PROTOCOL_VERSION: UInt8 = 0x01
    
    enum PacketType: UInt8 {
        case handshakeNotify = 0x01
        case handshakeAck = 0x02
        case poseData = 0x10
        case calibStart = 0xD1
        case calibSuccess = 0xD2
        case calibAbort = 0xD3
        case debugAxisToggle = 0xD4
        case swordGrab = 0xD5
        case swordDrop = 0xD6
        case ping = 0xBB
        case pong = 0xCC
    }
    
    private let LISTENING_PORT: UInt16 = 5000 // For Handshake
    private let VISION_PRO_PORT: UInt16 = 5001 // For Data
    
    private var sequenceNumber: UInt16 = 0
    
    // MARK: - Lifecycle
    
    init() {
        // Start listening for handshakes immediately
        startListening(on: LISTENING_PORT)
        
        // Setup packet processing sink
        dataReceived
            .sink { [weak self] (data, endpoint) in
                self?.handlePacket(data: data, from: endpoint)
            }
            .store(in: &cancellables)
    }
    
    func makeHeader(type: PacketType) -> Data {
        var data = Data()
        data.append(PROTOCOL_VERSION)
        data.append(type.rawValue)
        var seq = sequenceNumber.littleEndian
        data.append(Data(bytes: &seq, count: 2))
        sequenceNumber &+= 1
        
        let timeMs = UInt32((ProcessInfo.processInfo.systemUptime * 1000).truncatingRemainder(dividingBy: Double(UInt32.max)))
        var tLE = timeMs.littleEndian
        data.append(Data(bytes: &tLE, count: 4))
        
        return data // 8 bytes
    }
    
    private func handlePacket(data: Data, from endpoint: NWEndpoint) {
        // v2 Packet Header: [Version(1)][Type(1)][Seq(2)][TS(4)] = 8 bytes minimum
        guard data.count >= 8 else {
            // Legacy/Tiny packets support (if needed)
            return
        }
        
        let bytes = [UInt8](data)
        let version = bytes[0]
        guard version == PROTOCOL_VERSION else { return }
        
        let typeRaw = bytes[1]
        guard let type = PacketType(rawValue: typeRaw) else { return }
        
        switch type {
        case .pong:
            // Handled or ignored
            break
        case .ping:
            // [0x01][0xBB][Seq(2)][TS(4)] -> Response with Pong
            var response = makeHeader(type: .pong)
            response.append(data.subdata(in: 4..<8)) // Echo original TS
            send(response)
            
        case .calibSuccess:
            print("Received Calibration Success from Vision Pro!")
            DispatchQueue.main.async {
                self.onCalibrationSuccess?()
            }
            
        case .calibAbort:
            let code = data.count > 4 ? bytes[4] : 0
            print("Received Calibration Abort (Code \(code)) from Vision Pro")
            DispatchQueue.main.async {
                let reason: String
                switch code {
                case 1: reason = "Stability Lost (Pos)"
                case 2: reason = "Stability Lost (Rot)"
                case 3: reason = "Phone Tracking Lost"
                case 4: reason = "Hand Tracking Lost"
                case 5: reason = "Timeout"
                default: reason = "Unknown Error"
                }
                self.onCalibrationAbort?(reason)
            }
            
        case .handshakeNotify:
            // [0x01][0x01][Seq(2)][TS(4)] -> IP Notify from Vision Pro
            print("Received Handshake IP Notify from \(endpoint)")
            
            if case let .hostPort(host, _) = endpoint {
                let ipStr: String
                if case let .ipv4(ipv4) = host {
                    ipStr = ipv4.debugDescription.replacingOccurrences(of: "%en0", with: "")
                } else {
                    ipStr = host.debugDescription
                }
                
                let cleanIP = ipStr.contains(":") ? ipStr : (ipStr.components(separatedBy: "%").first ?? ipStr)
                
                DispatchQueue.main.async {
                    self.visionProIP = cleanIP
                    self.isConnectedToVisionPro = true
                    print("UDPManager: Connecting to VP at \(cleanIP)")
                    self.connectTo(host: cleanIP, port: self.VISION_PRO_PORT)
                    self.sendAck()
                }
            }
        default:
            break
        }
    }
    
    private func sendAck() {
        let packet = makeHeader(type: .handshakeAck)
        send(packet)
        print("Sent Handshake ACK to Vision Pro")
    }

    // MARK: - Server (Listening)
    
    func startListening(on port: UInt16) {
        do {
            let params = NWParameters.udp
            // Allow local network access
            params.allowLocalEndpointReuse = true
            
            self.listener = try NWListener(using: params, on: NWEndpoint.Port(integerLiteral: port))
            
            self.listener?.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    print("UDP Listener ready on port \(port)")
                    DispatchQueue.main.async {
                        self?.isReady = true
                        self?.listeningPort = port
                    }
                case .failed(let error):
                    print("UDP Listener failed: \(error)")
                    DispatchQueue.main.async { self?.isReady = false }
                default:
                    break
                }
            }
            
            self.listener?.newConnectionHandler = { [weak self] newConnection in
                self?.handleIncomingConnection(newConnection)
            }
            
            self.listener?.start(queue: queue)
            
        } catch {
            print("Failed to create UDP listener: \(error)")
        }
    }
    
    private func handleIncomingConnection(_ connection: NWConnection) {
        connection.start(queue: queue)
        receive(on: connection)
    }
    
    private func receive(on connection: NWConnection) {
        connection.receiveMessage { [weak self] (data, context, isComplete, error) in
            if let data = data, !data.isEmpty {
                // Pass data up
                if let endpoint = connection.currentPath?.remoteEndpoint {
                    self?.dataReceived.send((data, endpoint))
                }
                
                // Also support endpoint from connection descriptor if currentPath invalid?
                // currentPath.remoteEndpoint is usually correct for UDP
            }
            
            if error == nil {
                // Continue receiving
                self?.receive(on: connection)
            } else {
                // Connection error or end
                connection.cancel()
            }
        }
    }
    
    func stopListening() {
        listener?.cancel()
        listener = nil
        isReady = false
    }
    
    func disconnect() {
        senderConnection?.cancel()
        senderConnection = nil
        listener?.cancel()
        listener = nil
        
        DispatchQueue.main.async {
            self.isConnectedToVisionPro = false
            self.visionProIP = nil
            self.isReady = false
        }
    }
    
    // MARK: - Client (Sending)
    
    private var senderConnection: NWConnection?
    private var currentTargetHost: String?
    private var currentTargetPort: UInt16?
    
    func connectTo(host: String, port: UInt16) {
        // If target changed, recreate connection
        if host == currentTargetHost && port == currentTargetPort && senderConnection?.state == .ready {
            return
        }
        
        senderConnection?.cancel()
        
        let hostEndpoint = NWEndpoint.Host(host)
        let portEndpoint = NWEndpoint.Port(integerLiteral: port)
        
        let conn = NWConnection(host: hostEndpoint, port: portEndpoint, using: .udp)
        
        conn.stateUpdateHandler = { state in
            switch state {
            case .ready:
                 print("UDP Sender ready to \(host):\(port)")
                break
            case .failed(let err):
                print("UDP Sender failed: \(err)")
            default:
                break
            }
        }
        
        conn.start(queue: queue)
        self.senderConnection = conn
        self.currentTargetHost = host
        self.currentTargetPort = port
        
        // 🔹 Fix: Listen for return signals (Success/Abort) on the data channel
        self.startReceiving(on: conn)
    }
    
    private func startReceiving(on conn: NWConnection) {
        conn.receiveMessage { [weak self] (data, context, isComplete, error) in
            if let data = data, !data.isEmpty {
                // Use existing handlePacket logic
                // We don't have an endpoint here since it's a connected socket, 
                // but handlePacket only uses it for handshakeNotify which won't happen here.
                let dummyEndpoint = NWEndpoint.hostPort(host: "0.0.0.0", port: 0)
                self?.handlePacket(data: data, from: dummyEndpoint)
            }
            if error == nil {
                self?.startReceiving(on: conn)
            }
        }
    }
    
    func send(_ data: Data) {
        guard let conn = senderConnection else { return }
        
        conn.send(content: data, completion: .contentProcessed({ error in
            if let error = error {
                print("UDP Send Error: \(error)")
            }
        }))
    }
    
    func sendCalibrationSignal() {
        let packet = makeHeader(type: .calibStart)
        send(packet)
        print("Sent Enter Calibration Signal (Protocol V1) to Vision Pro")
    }
    
    func sendDebugAxisToggle() {
        let packet = makeHeader(type: .debugAxisToggle)
        send(packet)
        print("Sent Debug Axis Toggle (Type 0xD4) to Vision Pro")
    }
    
    func sendGrabSignal() {
        let packet = makeHeader(type: .swordGrab)
        send(packet)
        DispatchQueue.main.async { self.swordMode = .HELD }
        print("Sent Sword GRAB (Type 0xD5) to Vision Pro")
    }
    
    func sendDropSignal() {
        let packet = makeHeader(type: .swordDrop)
        send(packet)
        DispatchQueue.main.async { self.swordMode = .DROPPED }
        print("Sent Sword DROP (Type 0xD6) to Vision Pro")
    }
    
    // MARK: - Utils
    
    static func getLocalIP() -> String? {
        var address: String?
        
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0 else { return nil }
        guard let firstAddr = ifaddr else { return nil }
        
        for ptr in sequence(first: firstAddr, next: { $0.pointee.ifa_next }) {
            let flags = Int32(ptr.pointee.ifa_flags)
            let addr = ptr.pointee.ifa_addr.pointee
            
            if (flags & (IFF_UP|IFF_RUNNING|IFF_LOOPBACK)) == (IFF_UP|IFF_RUNNING) {
                if addr.sa_family == UInt8(AF_INET) { // IPv4
                    var hostname = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                    if (getnameinfo(ptr.pointee.ifa_addr, socklen_t(addr.sa_len), &hostname, socklen_t(hostname.count), nil, socklen_t(0), NI_NUMERICHOST) == 0) {
                        address = String(cString: hostname)
                        let name = String(cString: ptr.pointee.ifa_name)
                        if name == "en0" {
                            break 
                        }
                    }
                }
            }
        }
        
        freeifaddrs(ifaddr)
        return address
    }
}
