//
//  MotionStreamer.swift
//  IMUSender
//
//  Created by sjh354 on 1/29/26.
//

import Foundation
import CoreMotion
import QuartzCore
import Combine

final class MotionStreamer: ObservableObject {
    private let motion = CMMotionManager()
    private let webrtc: WebRTCClient
    private var seq: UInt32 = 0
    @Published var isRunning: Bool = false

    init(webrtc: WebRTCClient) {
        self.webrtc = webrtc
    }

    func start(hz: Double = 100.0) {
        guard motion.isDeviceMotionAvailable else {
            print("DeviceMotion not available")
            return
        }
        if isRunning { return }
        // Mark running immediately to avoid double-start races.
        isRunning = true

        motion.deviceMotionUpdateInterval = 1.0 / hz
        let q = OperationQueue()
        q.qualityOfService = .userInteractive

        // 참고: xArbitraryZVertical은 “Z를 중력축으로” 맞춰주는 편이라 데모에 무난
        motion.startDeviceMotionUpdates(using: .xArbitraryZVertical, to: q) { [weak self] dm, err in
            guard let self = self, self.isRunning, let dm = dm, err == nil else { return }
            
            
            print("motion tick", dm.attitude.quaternion.w)

            // 52 bytes packet:
            // [seq u32][t f64][quat w,x,y,z f32x4][gyro f32x3][userAcc f32x3]
            self.seq &+= 1
            let t = CACurrentMediaTime() // monotonic

            let quat = dm.attitude.quaternion
            let gyro = dm.rotationRate         // rad/s
            let acc  = dm.userAcceleration     // g

            var packet = Data()
            packet.reserveCapacity(52)

            var seqLE = self.seq.littleEndian
            packet.append(Data(bytes: &seqLE, count: 4))

            var t64 = t
            packet.append(Data(bytes: &t64, count: 8))

            var qw = Float(quat.w), qx = Float(quat.x), qy = Float(quat.y), qz = Float(quat.z)
            packet.append(Data(bytes: &qw, count: 4))
            packet.append(Data(bytes: &qx, count: 4))
            packet.append(Data(bytes: &qy, count: 4))
            packet.append(Data(bytes: &qz, count: 4))

            var gx = Float(gyro.x), gy = Float(gyro.y), gz = Float(gyro.z)
            packet.append(Data(bytes: &gx, count: 4))
            packet.append(Data(bytes: &gy, count: 4))
            packet.append(Data(bytes: &gz, count: 4))

            var ax = Float(acc.x), ay = Float(acc.y), az = Float(acc.z)
            packet.append(Data(bytes: &ax, count: 4))
            packet.append(Data(bytes: &ay, count: 4))
            packet.append(Data(bytes: &az, count: 4))

            self.webrtc.sendBinary(packet)
        }

    }

    func stop() {
        // Flip state first so any in-flight callbacks stop sending.
        isRunning = false
        motion.stopDeviceMotionUpdates()
    }
}
