import SwiftUI
import UIKit

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var webrtc: WebRTCClient
    @StateObject private var motion: ARPoseStreamer

    @State private var hzText: String = "100"
    @State private var signalingUrl: String = "ws://3.37.140.87:8080"
    @State private var signalingRoom: String = "imu"

    init() {
        let webrtc = WebRTCClient()
        _webrtc = StateObject(wrappedValue: webrtc)
        _motion = StateObject(wrappedValue: ARPoseStreamer(webrtc: webrtc))
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {

                Text("IMU Sender (iPhone = Answerer)")
                    .font(.title2).bold()

                Text("Status: \(webrtc.status)")
                Text("DataChannel state: \(webrtc.dcState)")

                Divider()

                Text("Signaling Server")
                    .font(.headline)

                TextField("ws://EC2_PUBLIC_IP:8080", text: $signalingUrl)
                    .textFieldStyle(.roundedBorder)

                TextField("room id", text: $signalingRoom)
                    .textFieldStyle(.roundedBorder)

                Button("Connect Signaling") {
                    let url = signalingUrl.trimmingCharacters(in: .whitespacesAndNewlines)
                    let room = signalingRoom.trimmingCharacters(in: .whitespacesAndNewlines)
                    webrtc.connectSignaling(urlString: url, room: room.isEmpty ? "imu" : room)
                }
                .buttonStyle(.borderedProminent)

                Divider()

                HStack {
                    TextField("Hz", text: $hzText)
                        .keyboardType(.numberPad)
                        .frame(width: 70)
                        .textFieldStyle(.roundedBorder)

                    Button(motion.isRunning ? "Stop AR" : "Start AR") {
                        if motion.isRunning {
                            motion.stop()
                        } else {
                            motion.start()
                        }
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
            .padding()
        }
        .onChange(of: scenePhase) { phase in
            if phase != .active {
                webrtc.disconnectSignaling()
                UIApplication.shared.isIdleTimerDisabled = false
            } else {
                UIApplication.shared.isIdleTimerDisabled = true
            }
        }
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
        }
    }
}
