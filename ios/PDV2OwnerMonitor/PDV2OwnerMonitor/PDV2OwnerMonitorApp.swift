import SwiftUI

@main
struct PDV2OwnerMonitorApp: App {
    @StateObject private var session = AppSession()

    var body: some Scene {
        WindowGroup {
            RootView(session: session)
        }
    }
}
