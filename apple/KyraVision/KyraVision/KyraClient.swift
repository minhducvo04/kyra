import Foundation
import Observation

/// Talks to the Kyra server running on the Mac.
///
/// The Mac is the whole backend: the router, the memory, the tools and the
/// Anthropic key all stay there, and the headset is a front door like the web
/// HUD and the CLI. That is why this file is small and has no opinions about
/// what a turn means - it moves text.
///
/// Two things make it work over the Wi-Fi rather than over localhost: the
/// server must be started with `KYRA_HOST=0.0.0.0`, and `KYRA_API_TOKEN` must
/// be set and entered here. Checkpoint routes refuse remote access without
/// a configured token; older routes retain the server's optional-token policy.
struct ChatOut: Codable, Sendable {
    var reply: String
    var backend: String
    var actualBackend: String?
    var path: String?
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case reply, backend, path, reason
        case actualBackend = "actual_backend"
    }

    /// "claude · tool" - what answered, and whether it used a tool. The web HUD
    /// shows the same badge; in AUTO mode it is the only way to tell.
    var badge: String? {
        guard let actualBackend else { return nil }
        return path == "tool" ? "\(actualBackend) · tool" : actualBackend
    }
}

struct Reminder: Codable, Sendable, Identifiable {
    let id: Int
    let text: String
    let dueAt: String?
    let done: Bool

    enum CodingKeys: String, CodingKey {
        case id, text, done
        case dueAt = "due_at"
    }

    /// Just the date. A headset is read at arm's length; a full ISO timestamp is noise.
    var day: String? { dueAt.map { String($0.prefix(10)) } }
}

struct AudioClip: Codable, Sendable {
    let b64: String
    let text: String
}

@Observable
@MainActor
final class KyraClient: WorkspaceAPI, LessonAPI {
    var connection: KyraConnection { KyraConnection(url: baseURL, token: token) }
    /// The Mac's address on the LAN, e.g. http://192.168.1.20:8420. Bonjour
    /// discovery is a later slice; typing it once is enough to get going.
    var baseURL: String {
        didSet { UserDefaults.standard.set(baseURL, forKey: "kyra.baseURL") }
    }

    var token: String {
        didSet { UserDefaults.standard.set(token, forKey: "kyra.token") }
    }

    init() {
        baseURL = UserDefaults.standard.string(forKey: "kyra.baseURL") ?? ""
        token = UserDefaults.standard.string(forKey: "kyra.token") ?? ""
    }

    private func request(_ path: String, body: Data? = nil) throws -> URLRequest {
        guard let url = URL(string: baseURL.trimmingCharacters(in: .whitespaces) + path),
              url.host != nil else { throw KyraError.badURL }
        var request = URLRequest(url: url)
        request.httpMethod = body == nil ? "GET" : "POST"
        request.httpBody = body
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        // The Mac may be asleep or the headset may have just woken; waiting beats
        // failing at the exact moment someone starts talking.
        request.timeoutInterval = 120
        return request
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        return URLSession(configuration: config)
    }()

    /// Which backend the server is set to (auto / claude / local).
    func backend() async throws -> String {
        let (data, response) = try await Self.session.data(for: try request("/api/backend"))
        try Self.check(response)
        struct Out: Codable { let backend: String }
        return try JSONDecoder().decode(Out.self, from: data).backend
    }

    /// One turn, streamed. `onToken` fires per text delta so the reply appears
    /// as it is written - time-to-first-token is what makes a companion feel
    /// live (docs/plans/2026-09-07-human-interface.md). Tool turns and the local
    /// model send no tokens at all and arrive whole in the final value, which is
    /// why the caller must handle an empty stream rather than an error.
    func send(_ message: String, onToken: @Sendable @escaping (String) -> Void) async throws -> ChatOut {
        let body = try JSONEncoder().encode(["message": message])
        let (bytes, response) = try await Self.session.bytes(for: try request("/api/chat/stream", body: body))
        try Self.check(response)

        var event = ""
        var payload = ""
        var final: ChatOut?

        func flush() throws {
            guard !payload.isEmpty else { return }
            let data = Data(payload.utf8)
            switch event {
            case "token":
                if let delta = try? JSONDecoder().decode(String.self, from: data) { onToken(delta) }
            case "done":
                final = try JSONDecoder().decode(ChatOut.self, from: data)
            case "error":
                let message = (try? JSONDecoder().decode(String.self, from: data)) ?? "the turn failed"
                throw KyraError.stream(message)
            default:
                break
            }
            event = ""
            payload = ""
        }

        for try await line in bytes.lines {
            if line.hasPrefix("event: ") {
                // A new event ends the previous block. SSE separates blocks with
                // a blank line, but AsyncLineSequence does not hand blank lines
                // back - so waiting for one concatenated every token onto the
                // following `done` object and made its JSON unparseable. That
                // was invisible on tool turns, which send no tokens at all, and
                // broke every streamed turn. Found by running it, not by reading it.
                try flush()
                event = String(line.dropFirst("event: ".count))
            } else if line.hasPrefix("data: ") {
                payload += line.dropFirst("data: ".count)
            } else if line.isEmpty {
                try flush()
            }
        }
        // And the last block is ended by the stream closing, not by anything in it.
        try flush()

        guard let final else { throw KyraError.stream("the reply ended before it finished") }
        return final
    }

    /// Speak text that has already been written, one sentence at a time.
    ///
    /// `onClip` fires per sentence as its audio arrives, so playback can start
    /// on the first one while the rest is still being synthesised - the same
    /// trick that took the browser's first spoken word from 4.45s to 3.61s
    /// (docs/voice-latency.md). Kokoro on the Mac rather than AVSpeechSynthesizer
    /// on device: her voice should be the same voice everywhere, and the round
    /// trip is on a LAN.
    func speak(_ text: String, onClip: @Sendable @escaping (Data) -> Void) async throws {
        let body = try JSONEncoder().encode(["text": text])
        let (bytes, response) = try await Self.session.bytes(for: try request("/api/speak", body: body))
        try Self.check(response)

        var event = ""
        var payload = ""

        func flush() {
            guard !payload.isEmpty else { return }
            if event == "audio",
               let clip = try? JSONDecoder().decode(AudioClip.self, from: Data(payload.utf8)),
               let wav = Data(base64Encoded: clip.b64) {
                onClip(wav)
            }
            event = ""
            payload = ""
        }

        for try await line in bytes.lines {
            if line.hasPrefix("event: ") {
                flush()   // see send(): blank lines are not delivered
                event = String(line.dropFirst("event: ".count))
            } else if line.hasPrefix("data: ") {
                payload += line.dropFirst("data: ".count)
            } else if line.isEmpty {
                flush()
            }
        }
        flush()
    }

    // ---- the daily loop -------------------------------------------------------
    // Read-only endpoints plus the two actions that close a loop. Everything else
    // (profile, documents, autofill) stays on the Mac: those are desk work, and
    // putting them on a headset would be interface for its own sake.

    func reminders() async throws -> [Reminder] {
        let (data, response) = try await Self.session.data(for: try request("/api/reminders"))
        try Self.check(response)
        struct Out: Codable { let reminders: [Reminder] }
        return try JSONDecoder().decode(Out.self, from: data).reminders
    }

    func dueReviews() async throws -> [LearningItem] {
        let (data, response) = try await Self.session.data(for: try request("/api/learning/due"))
        try Self.check(response)
        struct Out: Codable { let due: [LearningItem] }
        return try JSONDecoder().decode(Out.self, from: data).due
    }

    func completeReminder(_ id: Int) async throws {
        let (_, response) = try await Self.session.data(for: try request("/api/reminders/\(id)/complete", body: Data("{}".utf8)))
        try Self.check(response)
    }

    func markReviewed(_ id: Int, remembered: Bool) async throws {
        let body = try JSONEncoder().encode(["remembered": remembered])
        let (_, response) = try await Self.session.data(for: try request("/api/learning/\(id)/review", body: body))
        try Self.check(response)
    }

    func checkpoints() async throws -> [TaskCheckpoint] {
        let (data, response) = try await Self.session.data(for: try request("/api/checkpoints"))
        try Self.check(response, data: data)
        struct Out: Decodable { let checkpoints: [TaskCheckpoint] }
        return try JSONDecoder().decode(Out.self, from: data).checkpoints
    }

    func saveCheckpoint(_ checkpoint: TaskCheckpoint) async throws -> TaskCheckpoint {
        struct Draft: Encodable {
            let revision: Int
            let task: String
            let last_result: String
            let next_action: String
            let references: String
        }
        let body = try JSONEncoder().encode(Draft(revision: checkpoint.revision, task: checkpoint.task,
            last_result: checkpoint.lastResult, next_action: checkpoint.nextAction, references: checkpoint.references))
        let (data, response) = try await Self.session.data(for: try request("/api/checkpoints/\(checkpoint.id)", body: body))
        try Self.check(response, data: data)
        return try JSONDecoder().decode(TaskCheckpoint.self, from: data)
    }

    func saveLesson(requestID: UUID, summary: String, takeaway: String) async throws -> LearningItem {
        let body = try JSONEncoder().encode([
            "request_id": requestID.uuidString.lowercased(), "topic": "Queue capacity and server failures",
            "summary": summary, "key_takeaway": takeaway
        ])
        let (data, response) = try await Self.session.data(for: try request("/api/learning", body: body))
        try Self.check(response, data: data)
        return try JSONDecoder().decode(LearningItem.self, from: data)
    }

    /// Stop a reply that is still being generated. The server keeps running the
    /// turn otherwise, and would record the whole thing as if it had been heard.
    func cancel() async {
        guard let request = try? request("/api/chat/cancel", body: Data("{}".utf8)) else { return }
        _ = try? await Self.session.data(for: request)
    }

    private static func check(_ response: URLResponse, data: Data? = nil) throws {
        guard let http = response as? HTTPURLResponse else { return }
        if http.statusCode == 401 { throw KyraError.unauthorized }
        if http.statusCode != 200, let data {
            struct Failure: Decodable { struct Detail: Decodable { let message: String }; let error: Detail }
            if let failure = try? JSONDecoder().decode(Failure.self, from: data) {
                throw KyraError.rejected(http.statusCode, failure.error.message)
            }
            if http.statusCode == 422 { throw KyraError.rejected(422, "Check the field lengths and required values, then try again.") }
        }
        if http.statusCode != 200 { throw KyraError.server(http.statusCode) }
    }
}
