import Foundation

struct VoiceFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

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
    var badge: String? {
        guard let actualBackend else { return nil }
        return path == "tool" ? "\(actualBackend) · tool" : actualBackend
    }
}

enum VoiceEvent: Sendable {
    case transcript(String)
    case audio(Data)
    case done(ChatOut)
}

/// URLSession's lines omit empty SSE separators, so a new event also flushes.
struct VoiceStream {
    private var kind = ""
    private var payload = ""
    private var completed = false

    mutating func consume(_ line: String) throws -> VoiceEvent? {
        if line.hasPrefix("event: ") {
            let event = try flush()
            kind = String(line.dropFirst(7))
            return event
        }
        if line.hasPrefix("data: ") {
            if !payload.isEmpty { payload += "\n" }
            payload += line.dropFirst(6)
        }
        if line.isEmpty { return try flush() }
        return nil
    }

    mutating func finish() throws -> VoiceEvent? {
        let event = try flush()
        guard completed else { throw VoiceFailure("The voice reply ended early. Please try again.") }
        return event
    }

    private mutating func flush() throws -> VoiceEvent? {
        guard !payload.isEmpty else { return nil }
        defer { kind = ""; payload = "" }
        let data = Data(payload.utf8)
        let decoder = JSONDecoder()
        switch kind {
        case "transcript":
            struct Transcript: Decodable { let transcript: String }
            return .transcript(try decoder.decode(Transcript.self, from: data).transcript)
        case "audio":
            struct Clip: Decodable { let b64: String }
            let clip = try decoder.decode(Clip.self, from: data)
            guard let wav = Data(base64Encoded: clip.b64) else { throw VoiceFailure("Invalid reply audio.") }
            return .audio(wav)
        case "done":
            let reply = try decoder.decode(ChatOut.self, from: data)
            completed = true
            return .done(reply)
        case "error":
            throw VoiceFailure((try? decoder.decode(String.self, from: data)) ?? "The voice turn failed.")
        default: return nil
        }
    }
}

enum VoiceUpload {
    static func body(_ wav: Data, boundary: String) -> Data {
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
        body.append(wav)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        return body
    }
}
