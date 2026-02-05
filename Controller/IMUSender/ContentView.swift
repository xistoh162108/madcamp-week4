import SwiftUI
import UIKit

struct ContentView: View {
    @EnvironmentObject var udpManager: UDPManager
    @StateObject private var motion = MotionStreamer()
    @StateObject private var pairingService = PairingService()
    @State private var localIP: String = "Loading..."
    @State private var showCalibrationMarker = false
    @State private var abortReason: String? = nil
    
    var body: some View {
        ZStack {
            Color(UIColor.systemBackground).ignoresSafeArea()
            
            if !udpManager.isConnectedToVisionPro {
                // Handshake Mode (Pairing Code)
                VStack(spacing: 20) {
                    Spacer()
                    Text("Pairing Code").font(.headline).foregroundColor(.secondary)
                    Text(pairingService.pairingCode)
                        .font(.system(size: 60, weight: .bold, design: .monospaced))
                        .foregroundColor(.blue)
                        .padding()
                        .background(Color.blue.opacity(0.1))
                        .cornerRadius(20)
                    
                    if pairingService.isAdvertising {
                        Label(pairingService.statusMessage, systemImage: "antenna.radiowaves.left.and.right")
                            .font(.caption).foregroundColor(.green)
                    } else {
                        Label(pairingService.statusMessage, systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundColor(.red)
                    }
                    Spacer()
                    Text("Enter this code on Vision Pro").font(.body).foregroundColor(.gray)
                }
                .onAppear { if let ip = UDPManager.getLocalIP() { self.localIP = ip } }
            } else {
                // Connected Mode
                VStack(spacing: 30) {
                    Image(systemName: "checkmark.circle.fill")
                        .resizable().frame(width: 80, height: 80).foregroundColor(.green)
                    Text("Connected to Vision Pro").font(.title).bold()
                    HStack {
                        Text(udpManager.visionProIP ?? "Unknown IP").font(.monospaced(.body)())
                        Text(udpManager.swordMode == .HELD ? "HELD" : "DROPPED")
                            .font(.caption).bold()
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(udpManager.swordMode == .HELD ? Color.blue : Color.red)
                            .foregroundColor(.white).cornerRadius(6)
                    }
                    .foregroundColor(.secondary)
                    
                    if motion.isRunning {
                        VStack(spacing: 15) {
                            Text("Auto-Streaming Active").font(.headline).foregroundColor(.green)
                            Image(systemName: "antenna.radiowaves.left.and.right").font(.system(size: 40)).foregroundColor(.green)
                            
                            Button(action: {
                                abortReason = nil
                                udpManager.sendCalibrationSignal()
                                withAnimation { showCalibrationMarker = true }
                            }) {
                                HStack {
                                    Image(systemName: "scope")
                                    Text("Recalibrate")
                                }
                                .font(.headline).foregroundColor(.white).frame(maxWidth: .infinity).padding().background(Color.orange).cornerRadius(12)
                            }
                            .padding(.horizontal, 40)
                            
                            HStack(spacing: 20) {
                                Button(action: { udpManager.sendGrabSignal() }) {
                                    Label("Grab", systemImage: "hand.tap.fill")
                                        .font(.headline).padding().frame(maxWidth: .infinity).background(Color.blue).foregroundColor(.white).cornerRadius(12)
                                }
                                
                                Button(action: { udpManager.sendDropSignal() }) {
                                    Label("Drop", systemImage: "arrow.down.to.line.fill")
                                        .font(.headline).padding().frame(maxWidth: .infinity).background(Color.red).foregroundColor(.white).cornerRadius(12)
                                }
                            }
                            .padding(.horizontal, 40)
                        }
                    } else {
                        VStack(spacing: 20) { ProgressView(); Text("Initializing Stream...").foregroundColor(.secondary) }
                    }
                    
                    Spacer()
                    
                    // Logic Refinement: Axis Verification Toggle
                    Button(action: { udpManager.sendDebugAxisToggle() }) {
                        Label("Verify Axis Mode", systemImage: "move.3d")
                            .font(.caption).foregroundColor(.secondary)
                            .padding(8).background(Color.secondary.opacity(0.1)).cornerRadius(8)
                    }
                    .padding(.bottom, 5)
                    
                    Button(action: {
                        motion.stop()
                        udpManager.disconnect()
                        pairingService.reset()
                        udpManager.startListening(on: 5000)
                    }) {
                        Text("Disconnect").font(.headline).foregroundColor(.red).padding().frame(maxWidth: .infinity).background(Color.red.opacity(0.1)).cornerRadius(12)
                            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.red.opacity(0.3), lineWidth: 1))
                    }
                    .padding(.horizontal, 40).padding(.bottom, 20)
                }
                
                if showCalibrationMarker {
                    ZStack {
                        Color.black.ignoresSafeArea()
                        VStack(spacing: 30) {
                            Text(abortReason != nil ? "Calibration Failed" : "Neutral Pose Calibration")
                                .font(.title).bold().foregroundColor(abortReason != nil ? .red : .white)
                            
                            Image(systemName: abortReason != nil ? "exclamationmark.octagon.fill" : "hand.raised.fill")
                                .font(.system(size: 100)).foregroundColor(abortReason != nil ? .red : .orange)
                            
                            if let reason = abortReason {
                                Text(reason).font(.title2).bold().foregroundColor(.red).multilineTextAlignment(.center)
                                Text("Please try again and hold still.").font(.subheadline).foregroundColor(.gray)
                            } else {
                                Text("Hold your iPhone naturally\nin front of you in Vision Pro.").font(.headline).multilineTextAlignment(.center).foregroundColor(.white)
                                ProgressView().scaleEffect(1.5).tint(.orange).padding()
                            }
                            
                            Button("Cancel") { withAnimation { showCalibrationMarker = false; abortReason = nil } }
                                .buttonStyle(.bordered).tint(.white).padding(.top, 20)
                        }
                        .padding()
                    }
                    .transition(.opacity).zIndex(10)
                }
            }
        }
        .onAppear {
            if let ip = UDPManager.getLocalIP() { self.localIP = ip }
            UIApplication.shared.isIdleTimerDisabled = true
            
            udpManager.onCalibrationSuccess = {
                let generator = UINotificationFeedbackGenerator()
                generator.notificationOccurred(.success)
                withAnimation { showCalibrationMarker = false; abortReason = nil }
                if !motion.isRunning { motion.start(udp: udpManager) }
            }
            
            udpManager.onCalibrationAbort = { reason in
                let generator = UINotificationFeedbackGenerator()
                generator.notificationOccurred(.error)
                withAnimation { self.abortReason = reason }
                Task {
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    await MainActor.run {
                        withAnimation { if self.abortReason == reason { self.abortReason = nil; showCalibrationMarker = false } }
                    }
                }
            }
        }
        .onChange(of: udpManager.isConnectedToVisionPro) { connected in
            if connected && !motion.isRunning { motion.start(udp: udpManager) }
        }
    }
}
