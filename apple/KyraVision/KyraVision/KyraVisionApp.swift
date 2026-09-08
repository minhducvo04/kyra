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
    /// `--args -kyra.tab today` opens straight to Today. Same seam as -kyra.ask:
    /// it is how the app can be driven without a keyboard, and the shape a
    /// Shortcut ("Kyra, what's due?") would use.
    @State private var tab = UserDefaults.standard.string(forKey: "kyra.tab") ?? "talk"

    var body: some Scene {
        WindowGroup {
            // A TabView, because Apple's visionOS guidance is to start with what
            // people already recognise. Talking to her is the default tab; Today
            // is the part of the morning digest that is acted on rather than read.
            TabView(selection: $tab) {
                Tab("Talk", systemImage: "waveform", value: "talk") {
                    ContentView(client: client)
                }
                Tab("Today", systemImage: "checklist", value: "today") {
                    TodayView(client: client)
                }
            }
        }
        .defaultSize(width: 620, height: 760)
    }
}
