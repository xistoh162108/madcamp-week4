
//
//  PairingService.swift
//  IMUSender
//
//  Created by Auto-Agent on 2/2/26.
//

import Foundation
import Combine

class PairingService: NSObject, ObservableObject, NetServiceDelegate {
    @Published var pairingCode: String = "----"
    @Published var isAdvertising: Bool = false
    @Published var statusMessage: String = "Initializing..."
    
    private var netService: NetService?
    
    override init() {
        super.init()
        startAdvertising()
    }
    
    func generateCode() -> String {
        let code = String(Int.random(in: 1000...9999))
        self.pairingCode = code
        return code
    }
    
    func startAdvertising() {
        let code = generateCode()
        
        // Use standard Bonjour strings (no trailing dots)
        // Domain can be empty string for default (local.)
        netService = NetService(domain: "", type: "_madcamp._tcp", name: "iPhoneController", port: 5002)
        
        guard let service = netService else {
            self.statusMessage = "Failed to create NetService"
            return
        }
        
        service.delegate = self
        
        // Set TXT Record
        let txtDict: [String: Data] = ["code": code.data(using: .utf8)!]
        let txtData = NetService.data(fromTXTRecord: txtDict)
        service.setTXTRecord(txtData)
        
        // Publish
        service.publish()
        
        print("PairingService: Attempting to publish _madcamp._tcp code \(code)")
    }
    
    func stopAdvertising() {
        netService?.stop()
        netService = nil
        isAdvertising = false
    }
    
    func reset() {
        stopAdvertising()
        startAdvertising()
    }
    
    // MARK: - NetServiceDelegate
    
    func netServiceDidPublish(_ sender: NetService) {
        DispatchQueue.main.async {
            self.isAdvertising = true
            self.statusMessage = "Discoverable"
            print("PairingService: Successfully published Bonjour service")
        }
    }
    
    func netService(_ sender: NetService, didNotPublish errorDict: [String : NSNumber]) {
        DispatchQueue.main.async {
            self.isAdvertising = false
            let error = errorDict[NetService.errorCode]?.intValue ?? -1
            self.statusMessage = "Failed: NetService Error \(error)"
            print("PairingService: Failed to publish - \(errorDict)")
        }
    }
}
