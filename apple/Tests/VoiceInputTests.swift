import XCTest
@testable import KyraQueueLab

@MainActor final class FakeVoiceRecorder: VoiceRecording {
    var allow: CheckedContinuation<Bool, Never>?
    var onFinish: ((Bool) -> Void)?
    var active = false
    var failStart = false
    func requestPermission() async -> Bool {
        await withCheckedContinuation { allow = $0 }
    }
    func start() throws {
        if failStart { throw URLError(.cannotOpenFile) }
        active = true
    }
    func finish() throws -> Data { active = false; return Data([1, 2, 3]) }
    func cancel() { active = false }
}

final class VoiceInputTests: XCTestCase {
    @MainActor func testCancelledPermissionCannotStartMicrophone() async {
        let recorder = FakeVoiceRecorder()
        let input = VoiceInput(recorder: recorder)
        let task = Task { await input.start() }
        while recorder.allow == nil { await Task.yield() }
        input.cancel()
        recorder.allow?.resume(returning: true)
        await task.value
        XCTAssertFalse(recorder.active)
        XCTAssertEqual(input.phase, .idle)
        XCTAssertNil(input.finish())
    }

    @MainActor func testFinishIsSingleUseAndCancelDiscards() async {
        let recorder = FakeVoiceRecorder()
        let input = VoiceInput(recorder: recorder)
        let task = Task { await input.start() }
        while recorder.allow == nil { await Task.yield() }
        recorder.allow?.resume(returning: true)
        await task.value
        XCTAssertEqual(input.phase, .recording)
        XCTAssertEqual(input.finish(), Data([1, 2, 3]))
        XCTAssertNil(input.finish())
        XCTAssertFalse(recorder.active)
        input.cancel()
        XCTAssertNil(input.finish())
    }

    @MainActor func testRecordingLimitKeepsAudioForExplicitSend() async {
        let recorder = FakeVoiceRecorder()
        let input = VoiceInput(recorder: recorder)
        let task = Task { await input.start() }
        while recorder.allow == nil { await Task.yield() }
        recorder.allow?.resume(returning: true)
        await task.value
        recorder.onFinish?(true)
        XCTAssertEqual(input.phase, .ready)
        XCTAssertEqual(input.finish(), Data([1, 2, 3]))
        XCTAssertNil(input.finish())
    }

    @MainActor func testFailedCaptureDiscardsAndCancelledTaskResetsPermissionState() async {
        let recorder = FakeVoiceRecorder()
        let input = VoiceInput(recorder: recorder)
        let task = Task { await input.start() }
        while recorder.allow == nil { await Task.yield() }
        task.cancel()
        recorder.allow?.resume(returning: true)
        await task.value
        XCTAssertEqual(input.phase, .idle)
        XCTAssertFalse(recorder.active)
        recorder.allow = nil
        let retry = Task { await input.start() }
        while recorder.allow == nil { await Task.yield() }
        recorder.allow?.resume(returning: true)
        await retry.value
        recorder.onFinish?(false)
        XCTAssertEqual(input.phase, .idle)
        XCTAssertNotNil(input.error)
        XCTAssertNil(input.finish())
    }

    @MainActor func testDeniedPermissionAndFailedRecorderAreRetryable() async {
        for allowed in [false, true] {
            let recorder = FakeVoiceRecorder()
            recorder.failStart = true
            let input = VoiceInput(recorder: recorder)
            let task = Task { await input.start() }
            while recorder.allow == nil { await Task.yield() }
            recorder.allow?.resume(returning: allowed)
            await task.value
            XCTAssertEqual(input.phase, .idle)
            XCTAssertNotNil(input.error)
            XCTAssertFalse(recorder.active)
        }
    }
}
