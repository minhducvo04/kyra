import SwiftUI
import RealityKit
import Observation

struct LearningLabView: View {
    let client: KyraClient
    @Bindable var lab: LearningLab
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var confirmDiscard = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Learning lab").font(.largeTitle.bold())
                Text("What happens when a server goes offline?").font(.title2)
                Text("One shared queue, three servers, ten seconds. Each active server completes up to two requests per second.")
                    .foregroundStyle(.secondary)
                Button("Open spatial lab", systemImage: "cube.transparent") { openWindow(id: "queue-lab", value: "shared") }
                    .buttonStyle(.borderedProminent)

                VStack(alignment: .leading, spacing: 14) {
                    Stepper("Arrivals: \(lab.arrivals) requests / second", value: Binding(get: { lab.arrivals }, set: { lab.setArrivals($0) }), in: 0...10)
                    HStack {
                        ForEach(0..<3) { i in
                            Button { lab.toggle(i) } label: {
                                Label("Server \(i + 1)", systemImage: lab.servers[i] ? "checkmark.circle.fill" : "xmark.circle")
                                    .foregroundStyle(lab.servers[i] ? .mint : .orange)
                            }
                            .accessibilityLabel("Server \(i + 1), \(lab.servers[i] ? "online" : "offline"). Toggle server.")
                        }
                    }
                    Text("Prediction: how many requests will still be waiting after ten seconds?")
                        .font(.headline)
                    Stepper("I predict \(lab.prediction) waiting", value: $lab.prediction, in: 0...100)
                    Button("Commit prediction and run", systemImage: "play.fill") { lab.run() }
                        .buttonStyle(.borderedProminent)
                }.disabled(!lab.canConfigure)

                if let run = lab.result {
                    Button("New experiment", systemImage: "arrow.counterclockwise") {
                        if lab.hasUnsavedLesson { confirmDiscard = true } else { lab.discardLesson() }
                    }.disabled(lab.saving)
                    if lab.hasUnsavedLesson {
                        Text("Save your takeaway or start a new experiment to change the servers.").font(.caption).foregroundStyle(.secondary)
                    }
                    Divider()
                    Text(lab.capturedPrediction == run.final.waiting ? "Your prediction matched." : "Compare your prediction.")
                        .font(.title2.bold())
                    Text("Predicted \(lab.capturedPrediction) waiting · Measured \(run.final.waiting)")
                    Text(run.explanation)
                    HStack(alignment: .bottom, spacing: 8) {
                        ForEach(run.ticks, id: \.second) { tick in
                            VStack(spacing: 4) {
                                Text("\(tick.waiting)").font(.caption2)
                                RoundedRectangle(cornerRadius: 4).fill(.mint.opacity(0.8))
                                    .frame(height: max(3, CGFloat(tick.waiting) * 1.1))
                                Text("\(tick.second)s").font(.caption2).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity)
                        }
                    }.frame(height: 160, alignment: .bottom)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("Waiting requests at each second: " + run.ticks.map { "\($0.second): \($0.waiting)" }.joined(separator: ", "))
                    Text("Waiting after each tick. Arrivals happen first; servers then take work in numbered order. This model has no network delay or in-flight requests.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Keep a takeaway").font(.headline)
                    TextField("What would you like to remember?", text: $lab.takeaway, axis: .vertical)
                        .lineLimit(3...8).textFieldStyle(.roundedBorder)
                        .disabled(lab.saveAttempted)
                    Button(lab.saved ? "Saved for tomorrow's review" : (lab.saveAttempted ? "Retry saving the same lesson" : "Save for review"),
                           systemImage: lab.saved ? "checkmark.circle" : "bookmark") {
                        Task { await lab.save(using: client) }
                    }
                    .disabled(lab.saving || lab.saved || lab.takeaway.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    if lab.saving { ProgressView("Saving on your Mac…") }
                    if lab.saveAttempted && !lab.saved && !lab.saving {
                        Text("Your lesson is preserved for a safe retry. A new run starts a new lesson.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let problem = lab.problem { Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
            }.padding(30)
        }
        .confirmationDialog("Discard the unsaved lesson and start a new experiment?", isPresented: $confirmDiscard) {
            Button("Discard lesson", role: .destructive) { lab.discardLesson() }
        }
        #if DEBUG
        .task {
            // Visual smoke fixture only; never sends a request or saves a lesson.
            if UserDefaults.standard.bool(forKey: "kyra.lab.demo"), lab.result == nil {
                lab.toggle(2)
                lab.prediction = 10
                lab.run()
                // Remove restored smoke windows before opening the keyed volume.
                dismissWindow(id: "queue-lab")
                openWindow(id: "queue-lab", value: "shared")
            }
        }
        #endif
    }

}

/// A volume in Shared Space, so this can sit beside the user's other windows.
/// Geometry represents program state; rendering never advances the simulation.
struct QueueVolumeView: View {
    @Bindable var lab: LearningLab

    var body: some View {
        VStack(spacing: 8) {
            Text("Three servers, one queue").font(.system(size: 52, weight: .bold))
            Text(lab.canConfigure ? "Tap a server to take it offline. Predict and run in the Learning lab window." : "Result is frozen. Save your lesson or start a new experiment in the Learning lab window.")
                .font(.system(size: 28)).foregroundStyle(.secondary)
            RealityView { content in
                content.add(scene())
            } update: { content in
                content.entities.removeAll()
                content.add(scene())
            }
            .gesture(SpatialTapGesture().targetedToAnyEntity().onEnded { value in
                if let index = Int(value.entity.name), (0..<3).contains(index) { lab.toggle(index) }
            })
            .accessibilityLabel("Three-dimensional server model. Equivalent server controls are in the Learning lab window.")
            HStack(spacing: 36) {
                ForEach(0..<3) { i in
                    VStack {
                        Text("Server \(i + 1): \(lab.servers[i] ? "online" : "offline")")
                        if let run = lab.result { Text("\(run.final.byServer[i]) completed").foregroundStyle(.secondary) }
                    }.frame(maxWidth: .infinity)
                }
            }.font(.system(size: 30))
            if let run = lab.result {
                Text("\(run.final.waiting) waiting · Each orange block represents up to 10 requests")
                    .font(.system(size: 30, weight: .semibold))
            } else {
                Text("\(lab.arrivals) arrivals per second · Predict to reveal the result").font(.system(size: 30, weight: .semibold))
            }
        }
        .padding(40)
        .glassBackgroundEffect()
    }

    @MainActor private func scene() -> Entity {
        let root = Entity()
        for i in 0..<3 {
            let server = ModelEntity(mesh: .generateBox(size: 0.12, cornerRadius: 0.015),
                                     materials: [SimpleMaterial(color: lab.servers[i] ? .systemMint : .systemGray, isMetallic: false)])
            server.position = [Float(i - 1) * 0.23, -0.035, 0]
            server.name = String(i)
            server.components.set(InputTargetComponent())
            server.components.set(HoverEffectComponent())
            server.generateCollisionShapes(recursive: false)
            root.addChild(server)
        }
        if let run = lab.result {
            let blocks = (run.final.waiting + 9) / 10
            for i in 0..<blocks {
                let block = ModelEntity(mesh: .generateBox(size: 0.035, cornerRadius: 0.005),
                                        materials: [SimpleMaterial(color: .systemOrange, isMetallic: false)])
                let column = Float(i % 5) - Float(min(blocks, 5) - 1) / 2
                block.position = [column * 0.055, 0.07 + Float(i / 5) * 0.045, 0.04]
                root.addChild(block)
            }
        }
        return root
    }
}
