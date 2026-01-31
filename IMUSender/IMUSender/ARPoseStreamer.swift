//
//  ARPoseStreamer.swift
//  IMUSender
//
//  Created by sjh354 on 1/30/26.
//

import Foundation
import ARKit
import Combine
import simd

final class ARPoseStreamer: NSObject, ObservableObject {
    private let session = ARSession()
    private let webrtc: WebRTCClient
    private var seq: UInt32 = 0

    @Published var isRunning: Bool = false

    init(webrtc: WebRTCClient) {
        self.webrtc = webrtc
        super.init()
        session.delegate = self
    }

    func start() {
        guard ARWorldTrackingConfiguration.isSupported else {
            print("ARWorldTracking not supported on this device")
            return
        }
        if isRunning { return }
        isRunning = true

        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
    }

    func stop() {
        isRunning = false
        session.pause()
    }
}

extension ARPoseStreamer: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard isRunning else { return }

        // Pose from camera transform
        let t = frame.camera.transform
        let pos = SIMD3<Float>(t.columns.3.x, t.columns.3.y, t.columns.3.z)
        let q = simd_quatf(t)

        seq &+= 1

        // Packet (40 bytes):
        // [seq u32][t f64][pos x,y,z f32x3][quat x,y,z,w f32x4]
        var packet = Data()
        packet.reserveCapacity(40)

        var seqLE = seq.littleEndian
        packet.append(Data(bytes: &seqLE, count: 4))

        var t64 = frame.timestamp
        packet.append(Data(bytes: &t64, count: 8))

        var px = pos.x, py = pos.y, pz = pos.z
        packet.append(Data(bytes: &px, count: 4))
        packet.append(Data(bytes: &py, count: 4))
        packet.append(Data(bytes: &pz, count: 4))

        var qx = q.imag.x, qy = q.imag.y, qz = q.imag.z, qw = q.real
        packet.append(Data(bytes: &qx, count: 4))
        packet.append(Data(bytes: &qy, count: 4))
        packet.append(Data(bytes: &qz, count: 4))
        packet.append(Data(bytes: &qw, count: 4))

        webrtc.sendBinary(packet)
    }
}
