//
//  IMUSenderApp.swift
//  IMUSender
//
//  Created by sjh354 on 1/29/26.
//

import SwiftUI

@main
struct IMUSenderApp: App {
    @StateObject var udpManager = UDPManager()
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(udpManager)
        }
    }
}
