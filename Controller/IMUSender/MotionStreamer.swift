import Foundation
import ARKit
import QuartzCore
import Combine

final class MotionStreamer: NSObject, ObservableObject, ARSessionDelegate {
    private let session = ARSession()
    private var udpManager: UDPManager?
    @Published var isRunning: Bool = false
    private var sequenceNumber: UInt16 = 0

    func start(udp: UDPManager) {
        if isRunning { return }
        guard ARWorldTrackingConfiguration.isSupported else {
            print("MotionStreamer: ARKit not available")
            return
        }
        
        print("MotionStreamer: Starting ARSession...")
        isRunning = true
        self.udpManager = udp
        
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        session.delegate = self
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
    }

    func stop() {
        print("MotionStreamer: Stopping ARSession")
        isRunning = false
        session.pause()
    }
    
    private func getTrackingStateValue(_ session: ARSession) -> Int8 {
        guard let camera = session.currentFrame?.camera else { return 2 } // 2 = Not Available
        switch camera.trackingState {
        case .normal: return 0 // 0 = Normal
        case .limited: return 1 // 1 = Limited
        case .notAvailable: return 2 // 2 = Not Available
        }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard isRunning, let udp = udpManager else { return }
        
        // Logging Check
        if sequenceNumber % 60 == 0 {
            print("MotionStreamer: Sending Frame #\(sequenceNumber)")
        }
        
        // Pose
        let t = frame.camera.transform
        let pos =  SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let q = simd_quatf(t)
        
        // New Packet structure (38 bytes):
        // [Version(1)][Type(1)][Seq(2)][TS(4)] = 8 bytes (Header)
        // [Pos(12)][Rot(16)] = 28 bytes (Payload)
        // [State(1)][Spare(1)] = 2 bytes (Metadata)
        
        var packet = udp.makeHeader(type: .poseData) // Already adds Version, Type, Seq, TS (8 bytes)
        
        // 2. Position (12 bytes)
        var px = pos.x, py = pos.y, pz = pos.z
        packet.append(Data(bytes: &px, count: 4))
        packet.append(Data(bytes: &py, count: 4))
        packet.append(Data(bytes: &pz, count: 4))
        
        // 3. Rotation (16 bytes)
        var qx = q.imag.x, qy = q.imag.y, qz = q.imag.z, qw = q.real
        packet.append(Data(bytes: &qx, count: 4))
        packet.append(Data(bytes: &qy, count: 4))
        packet.append(Data(bytes: &qz, count: 4))
        packet.append(Data(bytes: &qw, count: 4))
        
        // 4. Metadata (2 bytes)
        var state = getTrackingStateValue(session)
        packet.append(Data(bytes: &state, count: 1))
        packet.append(UInt8(0)) // Spare
        
        udp.send(packet)
        sequenceNumber &+= 1
    }
}
