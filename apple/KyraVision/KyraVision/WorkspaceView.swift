import SwiftUI

/// Restoring context never executes a task or opens its references.
struct WorkspaceView: View {
    let client: KyraClient
    @State private var state: WorkspaceState
    @State private var confirmDiscard = false

    init(client: KyraClient) {
        self.client = client
        _state = State(initialValue: WorkspaceState(api: client))
    }

    var body: some View {
        @Bindable var state = state
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Where was I?").font(.largeTitle.bold())
                Text("Leave yourself a clear next step. Saved checkpoints live on your Mac.")
                    .foregroundStyle(.secondary)
                if let problem = state.problem { Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                if let notice = state.notice { Label(notice, systemImage: "checkmark.circle").foregroundStyle(.mint) }
                HStack {
                    Button("New checkpoint", systemImage: "plus") { state.clear() }
                        .disabled(state.dirty || state.busy)
                    Button("Refresh", systemImage: "arrow.clockwise") { Task { await state.load() } }
                        .disabled(state.dirty || state.busy)
                    if state.busy { ProgressView() }
                }
                if !state.checkpoints.isEmpty {
                    Text("SAVED TASKS").font(.caption).foregroundStyle(.secondary)
                    ForEach(state.checkpoints) { checkpoint in
                        Button { state.select(checkpoint) } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(checkpoint.task).font(.headline)
                                Text(checkpoint.nextAction).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
                        }.disabled(!state.canSelect)
                    }
                }
                Divider()
                Text(state.draft.revision == 0 ? "New checkpoint" : "Resume your task").font(.title2)
                VStack(alignment: .leading, spacing: 16) {
                    field("Task", text: $state.draft.task, prompt: "What are you working on?")
                    field("Last result", text: $state.draft.lastResult, prompt: "What did you finish or learn?")
                    field("Next action", text: $state.draft.nextAction, prompt: "One concrete step to start with")
                    field("References", text: $state.draft.references, prompt: "Optional links or file paths, one per line")
                    Text("References are saved as text. Kyra does not open or read them.")
                        .font(.caption).foregroundStyle(.secondary)
                }.disabled(!state.canEdit)
                HStack {
                    Button(state.retryPending ? "Retry same checkpoint" : "Save checkpoint", systemImage: "bookmark") {
                        Task { await state.save() }
                    }.buttonStyle(.borderedProminent).disabled(!state.canSave)
                    if state.conflict {
                        Button("Keep as new checkpoint") { state.keepAsNew() }.disabled(state.busy)
                    }
                    if state.dirty {
                        Button("Discard edits and reload") { confirmDiscard = true }.disabled(state.busy)
                    }
                }
                if state.retryPending {
                    Text("The save has not been confirmed. Retry the same checkpoint before editing; it will not create a duplicate.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(30)
        }
        .task(id: client.connection) { await state.connectionChanged() }
        .confirmationDialog("Discard your unsaved checkpoint edits?", isPresented: $confirmDiscard) {
            Button("Discard and reload", role: .destructive) {
                state.clear()
                Task { await state.load() }
            }
        }
    }

    private func field(_ label: String, text: Binding<String>, prompt: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.headline)
            TextField(prompt, text: text, axis: .vertical).lineLimit(2...5).textFieldStyle(.roundedBorder)
        }
    }
}
