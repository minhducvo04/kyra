import SwiftUI

/// The main window and optional learning volume coexist in Shared Space.
@main
struct KyraVisionApp: App {
    @State private var client = KyraClient()
    @State private var lab = LearningLab()
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
                Tab("Workspace", systemImage: "bookmark", value: "workspace") {
                    WorkspaceView(client: client)
                }
                Tab("Learn", systemImage: "cube.transparent", value: "learn") {
                    LearningLabView(client: client, lab: lab)
                }
            }
        }
        .defaultSize(width: 620, height: 760)

        WindowGroup(id: "queue-lab") {
            QueueVolumeView(lab: lab)
        }
        .windowStyle(.volumetric)
        .defaultSize(width: 0.85, height: 0.65, depth: 0.45, in: .meters)
        .defaultWindowPlacement { _, context in
            if let main = context.windows.first { return WindowPlacement(.trailing(main)) }
            return WindowPlacement()
        }
    }
}
