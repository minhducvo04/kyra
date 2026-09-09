import Foundation
import Observation

struct LearningItem: Codable, Sendable, Identifiable {
    let id: Int
    let topic: String
    let summary: String
    let keyTakeaway: String?
    enum CodingKeys: String, CodingKey {
        case id, topic, summary
        case keyTakeaway = "key_takeaway"
    }
}

@MainActor protocol LessonAPI {
    var connection: KyraConnection { get }
    func saveLesson(requestID: UUID, summary: String, takeaway: String) async throws -> LearningItem
}

@Observable @MainActor final class LearningLab {
    private(set) var arrivals = 5
    private(set) var servers = [true, true, true]
    var prediction = 0
    private(set) var result: QueueRun?
    private(set) var capturedPrediction = 0
    var takeaway = ""
    private(set) var requestID = UUID()
    private(set) var saveAttempted = false
    private(set) var saving = false
    private(set) var saved = false
    var problem: String?
    private var saveOrigin: KyraConnection?
    private var submittedTakeaway: String?

    var hasUnsavedLesson: Bool { result != nil && !saved }
    var canConfigure: Bool { !saving && !hasUnsavedLesson }

    /// Called only by the explicit new-experiment action (confirmed if unsaved).
    func discardLesson() {
        guard !saving else { return }
        result = nil; takeaway = ""; requestID = UUID()
        saveAttempted = false; saved = false; problem = nil
        saveOrigin = nil; submittedTakeaway = nil
    }

    func setArrivals(_ value: Int) {
        guard canConfigure, (0...10).contains(value) else { return }
        discardLesson(); arrivals = value
    }

    func toggle(_ index: Int) {
        guard canConfigure, (0..<3).contains(index) else { return }
        discardLesson(); servers[index].toggle()
    }

    func run() {
        guard canConfigure else { return }
        do {
            let measured = try SharedQueueSimulation().run(arrivals: arrivals, servers: servers)
            discardLesson()
            capturedPrediction = prediction; result = measured; takeaway = measured.explanation
        } catch { problem = "Choose three servers and an arrival rate from 0 to 10." }
    }

    func save(using api: any LessonAPI) async {
        guard !saving, !saved, let result else { return }
        guard saveOrigin == nil || saveOrigin == api.connection else {
            problem = "Restore the original Mac address and token to retry this lesson."
            return
        }
        let text = submittedTakeaway ?? takeaway
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let wasUncertain = saveAttempted
        saveOrigin = api.connection; submittedTakeaway = text
        saving = true; saveAttempted = true; problem = nil
        defer { saving = false }
        do {
            _ = try await api.saveLesson(requestID: requestID,
                summary: "Prediction: \(capturedPrediction) waiting. " + result.explanation, takeaway: text)
            saved = true
            if saveOrigin != api.connection { problem = "Saved on the original Mac, before the connection changed." }
        } catch {
            problem = error.localizedDescription
            if !wasUncertain, (error as? KyraError)?.definitiveFailure == true {
                saveAttempted = false; submittedTakeaway = nil; saveOrigin = nil
                requestID = UUID()
            }
        }
    }
}
