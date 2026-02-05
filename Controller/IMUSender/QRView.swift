//
//  QRView.swift
//  IMUSender
//
//  Created by Auto-Agent on 2/2/26.
//

import SwiftUI
import CoreImage.CIFilterBuiltins

struct QRView: View {
    let ipAddress: String
    let port: UInt16
    
    // URL Scheme Format
    // madcamp-controller://connect?ip=192.168.0.x&port=5000
    var qrString: String {
        return "madcamp-controller://connect?ip=\(ipAddress)&port=\(port)"
    }
    
    var body: some View {
        VStack(spacing: 20) {
            Text("Scan with Vision Pro")
                .font(.title2)
                .fontWeight(.bold)
            
            if let cgImage = generateQRCode(from: qrString) {
                Image(uiImage: UIImage(cgImage: cgImage))
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 250, height: 250)
                    .padding()
                    .background(Color.white)
                    .cornerRadius(12)
            } else {
                Image(systemName: "xmark.circle")
                    .resizable()
                    .frame(width: 200, height: 200)
                    .foregroundColor(.red)
            }
            
            VStack(alignment: .leading, spacing: 8) {
                Text("IP: \(ipAddress)")
                Text("Port: \(port)")
                Text("Payload: \(qrString)")
                    .font(.caption)
                    .foregroundColor(.gray)
            }
            .font(.system(.body, design: .monospaced))
        }
    }
    
    func generateQRCode(from string: String) -> CGImage? {
        let context = CIContext()
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        filter.correctionLevel = "M" // 15% error correction
        
        if let outputImage = filter.outputImage {
            return context.createCGImage(outputImage, from: outputImage.extent)
        }
        return nil
    }
    
    func calculateChecksum(_ input: String) -> Int {
        // Simple checksum for MVP: Sum of UTF8 checks
        // In production, use CRC16
        let data = input.data(using: .utf8) ?? Data()
        return data.reduce(0) { Int($0) + Int($1) } % 10000 // 4 digit max
    }
}
