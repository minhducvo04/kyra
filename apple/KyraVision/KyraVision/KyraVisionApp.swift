import SwiftUI

/// A plain window, deliberately.
///
/// Apple's visionOS guidance is to start with what people already recognise
/// rather than opening in a volume or full immersion, and the research behind
/// the human-interface plan says the same: the presence is the last thing to
/// build, not the first. So this is a window with a transcript in it, and the
/// orb comes only once the functional client is real.
@main
struct KyraVisionApp: App {
    @State private var client = KyraClient()

    var body: some Scene {
        WindowGroup {
            ContentView(client: client)
        }
        .defaultSize(width: 620, height: 760)
    }
}
