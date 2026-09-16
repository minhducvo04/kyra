import XCTest
@testable import KyraQueueLab

final class VoiceStreamTests: XCTestCase {
    func testVoiceEventsFlushWithoutBlankLinesAndAtEOF() throws {
        var stream = VoiceStream()
        let lines = ["event: transcript", "data: {\"transcript\":\"hello\"}",
                     "event: audio", "data: {\"b64\":\"AQID\",\"text\":\"Hi\"}",
                     "event: done", "data: {\"reply\":\"Hi\",\"backend\":\"auto\",\"actual_backend\":\"claude\"}"]
        var events: [VoiceEvent] = []
        for line in lines { if let event = try stream.consume(line) { events.append(event) } }
        if let event = try stream.finish() { events.append(event) }
        XCTAssertEqual(events.count, 3)
        guard case .transcript(let text) = events[0], case .audio(let audio) = events[1],
              case .done(let reply) = events[2] else { return XCTFail("Wrong event order") }
        XCTAssertEqual(text, "hello")
        XCTAssertEqual(audio, Data([1, 2, 3]))
        XCTAssertEqual(reply.reply, "Hi")
        XCTAssertEqual(reply.badge, "claude")
    }

    func testIncompleteStreamAndServerErrorFail() throws {
        var stream = VoiceStream()
        _ = try stream.consume("event: transcript")
        _ = try stream.consume("data: {\"transcript\":\"hello\"}")
        XCTAssertThrowsError(try stream.finish())
        var failed = VoiceStream()
        _ = try failed.consume("event: error")
        _ = try failed.consume("data: \"Transcription failed\"")
        XCTAssertThrowsError(try failed.finish())
    }

    func testSSEDataLinesCannotJoinSplitJSONTokens() throws {
        var stream = VoiceStream()
        _ = try stream.consume("event: done")
        _ = try stream.consume("data: {\"reply\":\"hi\",\"backend\":\"auto\",\"reason\":n")
        _ = try stream.consume("data: ull}")
        // SSE inserts a newline: n + newline + ull is invalid JSON, not null.
        XCTAssertThrowsError(try stream.finish())
    }

    func testMultipartPreservesWavBytesAndAudioField() {
        let wav = Data([0, 13, 10, 255, 42])
        let body = VoiceUpload.body(wav, boundary: "test-boundary")
        let prefix = Data("--test-boundary\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
        XCTAssertTrue(body.starts(with: prefix))
        XCTAssertEqual(body.subdata(in: prefix.count..<(prefix.count + wav.count)), wav)
        XCTAssertTrue(body.suffix(21).elementsEqual(Data("\r\n--test-boundary--\r\n".utf8)))
    }
}
