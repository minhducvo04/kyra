import SwiftUI

/// The main window and optional learning volume coexist in Shared Space.
@main
struct KyraVisionApp: App {
    @State private var companions = CompanionPresentation()
    @State private var client = KyraClient()
    @State private var presentation = OrbPresentation()
    @State private var speech = SpeechPlayer()
    @StateObject private var cast = CastClient()
    @StateObject private var space = CastSpaceModel()
    @StateObject private var sketch = SketchSpaceModel()
    @Environment(\.scenePhase) private var phase
    @State private var lab = LearningLab()
    @State private var showConnectionSettings = false
    /// `--args -kyra.tab today` opens straight to Today. Same seam as -kyra.ask:
    /// it is how the app can be driven without a keyboard, and the shape a
    /// Shortcut ("Kyra, what's due?") would use.
    @State private var tab: String = {
        #if DEBUG
        if CommandLine.arguments.contains("--verify-sketch") { return "sketch" }
        #endif
        return KyraMainTab.normalized(UserDefaults.standard.string(forKey: "kyra.tab"))
    }()

    var body: some Scene {
        Window("Kyra", id: "kyra-main") {
            // A TabView, because Apple's visionOS guidance is to start with what
            // people already recognise. Team is the default tab; Today
            // is the part of the morning digest that is acted on rather than read.
            TabView(selection: $tab) {
                Tab("Team", systemImage: "person.3", value: "team") {
                    PanelWindowView(panel: .team, client: client, voicePaused: showConnectionSettings)
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
                Tab("Sketch", systemImage: "scribble.variable", value: "sketch") {
                    SketchHomeView(sketch: sketch, castSpace: space)
                }
                Tab("3D", systemImage: "cube", value: "model3d") {
                    Model3DView(client: client)
                }
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button("Connection", systemImage: "gearshape") { showConnectionSettings = true }
                }
            }
            .sheet(isPresented: $showConnectionSettings) {
                ConnectionSettingsForm(client: client) { showConnectionSettings = false }
            }
            .modifier(CastArtifactDrop())
            .modifier(CompanionEntry(entry: tab))
            .environment(companions)
            .onChange(of: companions.tabRequest, initial: true) { _, request in
                if let request { tab = KyraMainTab.normalized(request.entry) }
            }
            .onChange(of: client.baseURL) { _, _ in cast.stop() }
            .onChange(of: client.token) { _, _ in cast.stop() }
            .ornament(attachmentAnchor: .scene(.bottom)) {
                PanelLauncher(space: space, cast: cast, sketch: sketch).environment(companions)
            }
        }
        .defaultSize(width: 620, height: 760)

        WindowGroup(id: "owl", for: String.self) { _ in
            OwlVolumeView(presence: presentation.presence, speech: speech).environment(companions).environmentObject(space)
        }
        .windowStyle(.volumetric)
        .windowResizability(.contentSize)
        .defaultSize(width: Double(PetLayout.owlVolume.x), height: Double(PetLayout.owlVolume.y),
                     depth: Double(PetLayout.owlVolume.z), in: .meters)
        .defaultWindowPlacement { _, context in
            if let main = context.windows.first(where: { $0.id == "kyra-main" }) {
                return WindowPlacement(.leading(main))
            }
            return WindowPlacement()
        }


        WindowGroup(id: "capybara", for: String.self) { _ in
            CapybaraVolumeView(presence: presentation.presence, speech: speech).environment(companions).environmentObject(space)
        }
        .windowStyle(.volumetric)
        .windowResizability(.contentSize)
        .defaultSize(width: Double(PetLayout.volume(for: .capybara).x),
                     height: Double(PetLayout.volume(for: .capybara).y),
                     depth: Double(PetLayout.volume(for: .capybara).z), in: .meters)
        .defaultWindowPlacement { _, context in
            if let main = context.windows.first(where: { $0.id == "kyra-main" }) {
                return WindowPlacement(.trailing(main))
            }
            return WindowPlacement()
        }

        CastArtifactScene(client: client, cast: cast)
        PanelWindowScene(client: client, companions: companions)
        CastWindowScene(client: client, cast: cast, space: space)
            .onChange(of: phase) { _, value in if value == .background { cast.stop() } }

        ImmersiveSpace(id: "kyra-space") { CastSpaceView(presence: presentation.presence, speech: speech, cast: cast, space: space).environment(companions) }
            .immersionStyle(selection: .constant(.mixed), in: .mixed)

        ImmersiveSpace(id: "kyra-sketch") { SketchSpaceView(sketch: sketch) }
            .immersionStyle(selection: .constant(.mixed), in: .mixed)

        WindowGroup(id: "perch", for: String.self) { $rawSpecies in
            if let rawSpecies, let species = PetSpecies(rawValue: rawSpecies) {
                PetPerchView(species: species, presence: presentation.presence, speech: speech)
                    .environment(companions).environmentObject(space)
            }
        }
        .windowStyle(.plain)
        .defaultSize(width: 560, height: 600)
        .defaultWindowPlacement { _, context in
            if let main = context.windows.first(where: { $0.id == "kyra-main" }) {
                return WindowPlacement(.trailing(main))
            }
            return WindowPlacement()
        }

        WindowGroup(id: "queue-lab", for: String.self) { _ in
            QueueVolumeView(lab: lab)
        }
        .windowStyle(.volumetric)
        .defaultSize(width: 0.85, height: 0.65, depth: 0.45, in: .meters)
        .defaultWindowPlacement { _, context in
            if let main = context.windows.first(where: { $0.id == "kyra-main" }) {
                return WindowPlacement(.trailing(main))
            }
            return WindowPlacement()
        }
    }
}

private struct PanelLauncher: View {
    @Environment(CompanionPresentation.self) private var companions
    @ObservedObject var space: CastSpaceModel
    @ObservedObject var cast: CastClient
    @ObservedObject var sketch: SketchSpaceModel
    @Environment(\.openWindow) private var openWindow
    @State private var followNotice = false
    private var windowActions = CompanionWindowActions()
    #if DEBUG
    @State private var fixtureStarted = false
    #endif
    #if DEBUG
    @Environment(\.openImmersiveSpace) private var openSpace
    @Environment(\.dismissImmersiveSpace) private var dismissSpace
    @Environment(\.dismissWindow) private var dismissWindow
    #endif

    private func launch(_ species: PetSpecies) {
        switch companions.handle(.launcherTapped(species)) {
        case .open(.owl): openWindow(id: "owl", value: "kyra-owl")
        case .open(.capybara): openWindow(id: "capybara", value: "kyra-capybara")
        case .refuse(.perched): space.message = "This companion is perched. Use Back to volume in its side controls."; followNotice = true
        case .refuse: space.message = "This companion is following. Use Stop or Exit in Kyra Space."; followNotice = true
        default: break
        }
    }

    var body: some View {
        HStack {
            Button("Owl", systemImage: "bird") {
                launch(.owl)
            }
            Button("Capybara", systemImage: "pawprint") {
                launch(.capybara)
            }
            CastSpaceButton(space: space)
            Button("Cast") { openWindow(id: "cast-connect") }
            ForEach(Panel.allCases, id: \.self) { panel in
                Button(panel.title) {
                    openWindow(id: "panel", value: panel)
                    // Reopening an existing panel need not trigger its onAppear.
                    switch companions.handle(.entryOpened(panel.rawValue)) {
                    case .open(.owl): openWindow(id: "owl", value: "kyra-owl")
                    case .open(.capybara): openWindow(id: "capybara", value: "kyra-capybara")
                    default: break
                    }
                }
            }
        }
        .padding(8)
        .glassBackgroundEffect()
        .alert("Companion already open", isPresented: $followNotice) { Button("OK", role: .cancel) {} } message: { Text(space.message) }
        #if DEBUG
        .task {
            guard !fixtureStarted else { return }; fixtureStarted = true
            if CommandLine.arguments.contains("--verify-sketch") {
                sketch.installSyntheticPreview()
                if CommandLine.arguments.contains("--verify-sketch-look") {
                    sketch.setMode(.lookAndSelect)
                }
                if case .opened = await openSpace(id: "kyra-sketch") {
                    dismissWindow(id: "kyra-main")
                }
                return
            }
            if CommandLine.arguments.contains("--verify-switch") { await verifySwitch(); return }
            if CommandLine.arguments.contains("--verify-dock") { await verifyDock(); return }
            if CommandLine.arguments.contains("--verify-follow") { await verifyFollow(); return }
            if let index = CommandLine.arguments.firstIndex(of: "--verify-perch"),
               CommandLine.arguments.indices.contains(index + 1),
               let species = PetSpecies(rawValue: CommandLine.arguments[index + 1]) {
                switch companions.handle(.switchRequested(species, to: .perch)) {
                case .openPerch: openWindow(id: "perch", value: species.rawValue)
                default: break
                }
                try? await Task.sleep(for: .seconds(1))
                dismissWindow(id: "kyra-main")
                return
            }
            if CommandLine.arguments.contains("--verify-capybara") {
                openWindow(id: "capybara", value: "kyra-capybara")
                if CommandLine.arguments.contains("--owl-reopen") {
                    try? await Task.sleep(for: .seconds(1))
                    openWindow(id: "capybara", value: "kyra-capybara")
                }
                return
            }
            if CommandLine.arguments.contains("--verify-owl") {
                openWindow(id: "owl", value: "kyra-owl")
                if CommandLine.arguments.contains("--owl-reopen") {
                    try? await Task.sleep(for: .seconds(1))
                    openWindow(id: "owl", value: "kyra-owl")
                }
                return
            }
            if CommandLine.arguments.contains("--verify-artifact-window") {
                openWindow(id: "artifact", value: ArtifactDescriptor(id: UUID(uuidString: "00000000-0000-0000-0000-000000000126")!, version: 1))
                return
            }
            guard CommandLine.arguments.contains("--verify-cast-space"), space.preview == nil else { return }
            space.preview = CastSpacePreview(cast: cast)
            space.transitioning = true
            if case .opened = await openSpace(id: "kyra-space") { space.active = true }
            space.transitioning = false
            if CommandLine.arguments.contains("--verify-space-restore") {
                // Exercise the same actions as the Save/Load controls, in an isolated fixture store.
                let original = space.layout
                space.save()
                var moved = space.layout.slots[0]
                moved.transform.position.x -= 0.2; moved.sizeMeters *= 0.8
                space.update(moved); space.load(original.id)
                assert(space.layout == original)
                if let stream = cast.streams.values.first(where: { $0.source == "counter-b" }) { cast.closeStream(stream.id) }
                space.message = "Synthetic proof: saved, changed, restored; counter-b missing."
            }
            if CommandLine.arguments.contains("--verify-space-exit") {
                let before = cast.streams.mapValues { ObjectIdentifier($0.layer) }
                try? await Task.sleep(for: .seconds(3))
                await dismissSpace()
                try? await Task.sleep(for: .seconds(3))
                assert(!space.active && !space.transitioning && cast.ids.count == 2)
                assert(cast.streams.allSatisfy { before[$0.key] == ObjectIdentifier($0.value.layer) && $0.value.layer.superlayer != nil })
                cast.status = "Synthetic exit verified: same two layers in Cast windows."
                FileHandle.standardOutput.write(Data("SPACE_EXIT_OK streams=2 same_layers=2\n".utf8))
            }
        }
        #endif
    }
    #if DEBUG
    private func verifySwitch() async {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: "--verify-switch"), args.indices.contains(index + 1),
              let species = PetSpecies(rawValue: args[index + 1]) else { return }
        launch(species)
        guard await awaitPresentation(species, mode: .volume) else { return }
        windowActions.apply(companions.handle(.switchRequested(species, to: .perch)))
        guard await awaitPresentation(species, mode: .perch) else { return }
        printSwitch(species, stage: "perch")
        try? await Task.sleep(for: .seconds(6))
        windowActions.apply(companions.handle(.switchRequested(species, to: .volume)))
        guard await awaitPresentation(species, mode: .volume) else { return }
        printSwitch(species, stage: "volume")
    }

    private func awaitPresentation(_ species: PetSpecies, mode: PetMode) async -> Bool {
        for _ in 0..<100 {
            let life = companions.lifecycle
            if life.presentation(of: species) == mode, life.pendingSwitch[species] == nil,
               (mode == .volume ? !life.perched.contains(species) : !life.visible.contains(species)) { return true }
            try? await Task.sleep(for: .milliseconds(100))
        }
        FileHandle.standardOutput.write(Data("SWITCH timeout species=\(species.rawValue) mode=\(mode)\n".utf8))
        return false
    }

    private func printSwitch(_ species: PetSpecies, stage: String) {
        let life = companions.lifecycle
        FileHandle.standardOutput.write(Data("SWITCH species=\(species.rawValue) stage=\(stage) perch_visible=\(life.perched.contains(species) ? 1 : 0) volume_visible=\(life.visible.contains(species) ? 1 : 0)\n".utf8))
    }

    private func verifyDock() async {
        let species: PetSpecies = CommandLine.arguments.contains("--dock-capybara") ? .capybara : .owl
        launch(species)
        guard await awaitPresentation(species, mode: .volume) else { return }
        companions.navigate(to: "talk")
        openWindow(id: "kyra-main")
        try? await Task.sleep(for: .seconds(1))
        companions.navigate(to: "talk")
        openWindow(id: "kyra-main")
        FileHandle.standardOutput.write(Data("DOCK species=\(species.rawValue) entries=\(PetDock.entries(for: species).joined(separator: ",")) request=talk repeated_open=2\n".utf8))
    }

    private func verifyFollow() async {
        let species: PetSpecies = CommandLine.arguments.contains("--follow-capybara") ? .capybara : .owl
        space.layout.slots = [] // Isolate the synthetic pet capture from desk-plane occlusion. Never saved.
        launch(species)
        try? await Task.sleep(for: .seconds(2))
        guard case .openSpace = companions.handle(.followRequested(species, spaceActive: false, transitioning: false)) else { return }
        space.transitioning = true
        let opened: Bool
        if case .opened = await openSpace(id: "kyra-space") { opened = true; space.active = true }
        else { opened = false }
        let result = companions.handle(.spaceOpened(opened))
        FileHandle.standardOutput.write(Data("FOLLOW opened=\(opened) restore=\(companions.lifecycle.restoreOnExit) species=\(species)".utf8)); FileHandle.standardOutput.write(Data([10]))
        switch result {
        case .dismiss(.owl): dismissWindow(id: "owl", value: "kyra-owl")
        case .dismiss(.capybara): dismissWindow(id: "capybara", value: "kyra-capybara")
        default: break
        }
        space.transitioning = false
        if CommandLine.arguments.contains("--follow-exit") {
            try? await Task.sleep(for: .seconds(17))
            await dismissSpace()
            try? await Task.sleep(for: .seconds(2))
            FileHandle.standardOutput.write(Data("FOLLOW exited=\(!space.active) following_nil=\(companions.lifecycle.following == nil) restored=\(companions.lifecycle.visible.contains(species))".utf8)); FileHandle.standardOutput.write(Data([10]))
        }
    }
    #endif

}
