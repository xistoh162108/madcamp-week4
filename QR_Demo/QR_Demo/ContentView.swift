
import SwiftUI
import ARKit
import RealityKit

struct ContentView: View {
    @State private var detectedQRCodes: [String] = []
    @State private var isScanning = false
    @State private var errorMessage: String?
    
    var body: some View {
        ZStack {
            RealityViewContainer()
            
            VStack(spacing: 16) {
                HStack {
                    Text("QR 감지: \(detectedQRCodes.count)개")
                        .font(.title2)
                    
                    Spacer()
                    
                    Button(action: toggleScanning) {
                        Image(systemName: isScanning ? "stop.circle.fill" : "play.circle.fill")
                            .font(.system(size: 24))
                    }
                }
                .padding()
                
                // 감지된 QR 코드 목록
                if !detectedQRCodes.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(detectedQRCodes.indices, id: \.self) { index in
                                HStack {
                                    Image(systemName: "qrcode")
                                        .foregroundColor(.blue)
                                    Text(detectedQRCodes[index])
                                        .font(.caption)
                                        .lineLimit(2)
                                }
                                .padding(8)
                                .background(Color.gray.opacity(0.1))
                                .cornerRadius(6)
                            }
                        }
                    }
                    .frame(maxHeight: 200)
                }
                
                if let error = errorMessage {
                    Text(error)
                        .foregroundColor(.red)
                        .font(.caption)
                }
                
                Spacer()
            }
            .padding()
        }
        .task {
            // Auto start
            if !isScanning {
                isScanning = true
                await startQRScanning()
            }
        }
        .onChange(of: isScanning) { _, newValue in
            if newValue {
                Task { await startQRScanning() }
            }
        }
    }
    
    private func toggleScanning() {
        isScanning.toggle()
    }
    
    private func startQRScanning() async {
        guard isScanning else { return }
        
        // 1. Session & Provider
        guard BarcodeDetectionProvider.isSupported else {
            DispatchQueue.main.async {
                errorMessage = "이 기기(또는 시뮬레이터)에서는\nQR 인식을 지원하지 않습니다."
            }
            return
        }

        let session = ARKitSession()
        let barcodeDetection = BarcodeDetectionProvider(symbologies: [.qr])
        
        do {
            // 2. Auth & Run
            _ = await session.queryAuthorization(for: [.worldSensing])
            try await session.run([barcodeDetection])
            
            // 3. Process Updates
            for await update in barcodeDetection.anchorUpdates {
                guard isScanning else { 
                    session.stop()
                    return 
                }
                
                if update.event == .added {
                    let anchor = update.anchor
                    // BarcodeAnchor has a payloadString property in visionOS 1.0+
                    if let payload = anchor.payloadString { 
                        DispatchQueue.main.async {
                            if !detectedQRCodes.contains(payload) {
                                detectedQRCodes.append(payload)
                            }
                        }
                    }
                }
            }
        } catch {
            print("ARKit Error: \(error)")
            DispatchQueue.main.async {
                errorMessage = "스캔 실패: \(error.localizedDescription)\n(권한을 확인하세요)"
            }
        }
    }
}

// Just a placeholder since VisionOS uses RealityView differently,
// but for the ZStack background, we can leave an empty RealityView or similar.
struct RealityViewContainer: View {
    var body: some View {
        RealityView { content in
            // Empty scene, just to have AR context if needed
        }
    }
}
