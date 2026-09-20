import Foundation
import Observation

struct KyraConnection: Equatable, Sendable {
    var url: String
    var token: String
}

struct TaskCheckpoint: Codable, Identifiable, Equatable, Sendable {
    var id = UUID().uuidString.lowercased()
    var revision = 0
    var task = ""
    var lastResult = ""
    var nextAction = ""
    var references = ""
    var updatedAt: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, revision, task, references
        case lastResult = "last_result", nextAction = "next_action", updatedAt = "updated_at"
    }
}

enum KyraError: LocalizedError {
    case badURL
    case unauthorized
    case server(Int)
    case rejected(Int, String)
    case stream(String)

    var errorDescription: String? {
        switch self {
        case .badURL: "That does not look like a URL. Try http://192.168.1.x:8420"
        case .unauthorized: "The server rejected the token. Check the connection settings."
        case .server(let code): "The Mac answered \(code)."
        case .rejected(_, let message), .stream(let message): message
        }
    }

    var rejectedStatus: Int? {
        if case .rejected(let status, _) = self, (400..<500).contains(status), status != 408 { return status }
        if case .unauthorized = self { return 401 }
        return nil
    }

    var definitiveFailure: Bool {
        if case .badURL = self { return true }
        return rejectedStatus != nil
    }
}

@MainActor protocol WorkspaceAPI {
    var connection: KyraConnection { get }
    func checkpoints() async throws -> [TaskCheckpoint]
    func saveCheckpoint(_ checkpoint: TaskCheckpoint) async throws -> TaskCheckpoint
}

@Observable @MainActor final class WorkspaceState {
    @ObservationIgnored private let api: any WorkspaceAPI
    private var origin: KyraConnection
    private var listOrigin: KyraConnection?
    private var original: TaskCheckpoint?
    private var pending: TaskCheckpoint?
    private var loadID = UUID()
    var checkpoints: [TaskCheckpoint] = []
    var draft = TaskCheckpoint()
    var busy = false
    var conflict = false
    var problem: String?
    var notice: String?

    init(api: any WorkspaceAPI) { self.api = api; origin = api.connection }

    var dirty: Bool {
        if let original { return draft != original }
        return !draft.task.isEmpty || !draft.lastResult.isEmpty || !draft.nextAction.isEmpty || !draft.references.isEmpty
    }
    var retryPending: Bool { pending != nil }
    var canEdit: Bool { !busy && pending == nil }
    var canSelect: Bool { !busy && !dirty && listOrigin == api.connection }
    var canSave: Bool {
        !busy && !conflict && origin == api.connection && (dirty || retryPending)
            && !draft.task.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !draft.nextAction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func clear() {
        guard !busy else { return }
        if listOrigin != api.connection { checkpoints = []; listOrigin = nil }
        draft = TaskCheckpoint(); original = nil; pending = nil
        origin = api.connection
        conflict = false; notice = nil; problem = nil
    }

    func select(_ checkpoint: TaskCheckpoint) {
        guard canSelect, checkpoints.contains(checkpoint) else { return }
        draft = checkpoint; original = checkpoint; origin = api.connection
        problem = nil; notice = "Resumed. Your next action is ready below."
    }

    func keepAsNew() {
        guard conflict && !busy else { return }
        draft.id = UUID().uuidString.lowercased(); draft.revision = 0; draft.updatedAt = nil
        original = nil; pending = nil; conflict = false; problem = nil
    }

    func connectionChanged() async {
        guard origin != api.connection || listOrigin == nil else { return }
        if dirty || retryPending || busy {
            problem = "The connection changed. Your draft is preserved; restore the original connection or discard edits to switch."
            return
        }
        clear()
        await load()
    }

    func load() async {
        guard !dirty && !retryPending else { return }
        let target = api.connection
        let generation = UUID()
        loadID = generation
        if listOrigin != target { checkpoints = []; listOrigin = nil; clear() }
        busy = true; problem = nil
        defer { if loadID == generation { busy = false } }
        do {
            let saved = try await api.checkpoints()
            guard loadID == generation, target == api.connection else { return }
            checkpoints = saved; listOrigin = target
        } catch {
            guard loadID == generation, target == api.connection else { return }
            problem = error.localizedDescription
        }
    }

    func save() async {
        guard canSave else { return }
        busy = true; problem = nil; notice = nil
        let target = origin
        let wasUncertain = pending != nil
        let submitted = pending ?? draft
        pending = submitted
        defer { busy = false }
        do {
            let saved = try await api.saveCheckpoint(submitted)
            // Record acknowledgement even if settings changed while awaiting it.
            // The draft and its origin remain paired; saving to another Mac stays blocked.
            draft = saved; original = saved; pending = nil
            if target == api.connection {
                if listOrigin != target { checkpoints = [] }
                checkpoints.removeAll { $0.id == saved.id }
                checkpoints.insert(saved, at: 0); listOrigin = target
                notice = "Saved on your Mac. Select this task whenever you want to resume."
            } else {
                problem = "Saved on the original Mac. Refresh to load the new connection."
            }
        } catch {
            problem = error.localizedDescription
            if !wasUncertain, let failure = error as? KyraError, failure.definitiveFailure {
                pending = nil
                conflict = failure.rejectedStatus == 409
            }
        }
    }
}
