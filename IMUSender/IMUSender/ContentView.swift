import SwiftUI
import UIKit
import AVFoundation

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var webrtc: WebRTCClient
    @StateObject private var motion: ARPoseStreamer

    @State private var hzText: String = "100"
    @State private var signalingRoom: String = "imu-data"
    @State private var showScanner: Bool = false

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

                Button("Scan QR") {
                    showScanner = true
                }
                .buttonStyle(.bordered)

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
        .sheet(isPresented: $showScanner) {
            ZStack(alignment: .topTrailing) {
                QRScannerView { code in
                    if let room = extractRoomId(from: code) {
                        signalingRoom = room
                        connectSignaling(with: room)
                    }
                    showScanner = false
                } onCancel: {
                    showScanner = false
                }
                Button("Close") {
                    showScanner = false
                }
                .padding()
                .background(.black.opacity(0.6))
                .foregroundColor(.white)
                .clipShape(Capsule())
                .padding()
            }
        }
    }

    private func extractRoomId(from text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        if let url = URL(string: trimmed),
           let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let room = comps.queryItems?.first(where: { $0.name == "room" })?.value,
           !room.isEmpty {
            return room
        }
        if let range = trimmed.range(of: "room=") {
            let after = trimmed[range.upperBound...]
            let room = after.split(separator: "&").first.map(String.init) ?? ""
            if !room.isEmpty { return room }
        }
        if trimmed.hasPrefix("room:") {
            let room = String(trimmed.dropFirst("room:".count))
            return room.isEmpty ? nil : room
        }
        return trimmed
    }

    private func connectSignaling(with room: String) {
        let trimmed = room.trimmingCharacters(in: .whitespacesAndNewlines)
        webrtc.connectSignaling(
            urlString: "wss://eokbba.shop",
            room: trimmed.isEmpty ? "imu" : trimmed
        )
    }
}

struct QRScannerView: UIViewControllerRepresentable {
    var onCode: (String) -> Void
    var onCancel: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onCode: onCode, onCancel: onCancel)
    }

    func makeUIViewController(context: Context) -> UIViewController {
        let vc = UIViewController()
        vc.view.backgroundColor = .black

        guard let device = AVCaptureDevice.default(for: .video) else {
            DispatchQueue.main.async { onCancel() }
            return vc
        }

        do {
            let input = try AVCaptureDeviceInput(device: device)
            let session = AVCaptureSession()
            session.addInput(input)

            let output = AVCaptureMetadataOutput()
            if session.canAddOutput(output) {
                session.addOutput(output)
                output.setMetadataObjectsDelegate(context.coordinator, queue: DispatchQueue.main)
                output.metadataObjectTypes = [.qr]
            }

            let preview = AVCaptureVideoPreviewLayer(session: session)
            preview.videoGravity = .resizeAspectFill
            preview.frame = vc.view.bounds
            vc.view.layer.addSublayer(preview)

            context.coordinator.session = session
            context.coordinator.previewLayer = preview
            session.startRunning()
        } catch {
            DispatchQueue.main.async { onCancel() }
        }

        return vc
    }

    func updateUIViewController(_ uiViewController: UIViewController, context: Context) {
        context.coordinator.previewLayer?.frame = uiViewController.view.bounds
    }

    static func dismantleUIViewController(_ uiViewController: UIViewController, coordinator: Coordinator) {
        coordinator.session?.stopRunning()
        coordinator.session = nil
        coordinator.previewLayer = nil
    }

    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate {
        let onCode: (String) -> Void
        let onCancel: () -> Void
        var session: AVCaptureSession?
        var previewLayer: AVCaptureVideoPreviewLayer?
        private var didSend = false

        init(onCode: @escaping (String) -> Void, onCancel: @escaping () -> Void) {
            self.onCode = onCode
            self.onCancel = onCancel
        }

        func metadataOutput(
            _ output: AVCaptureMetadataOutput,
            didOutput metadataObjects: [AVMetadataObject],
            from connection: AVCaptureConnection
        ) {
            guard !didSend else { return }
            if let obj = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
               obj.type == .qr,
               let value = obj.stringValue {
                didSend = true
                onCode(value)
            }
        }
    }
}
