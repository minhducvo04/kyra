import Foundation
import Observation
#if os(visionOS)
import AVFoundation
#endif

@MainActor protocol VoiceRecording: AnyObject {
    var onFinish: ((Bool) -> Void)? { get set }
    func requestPermission() async -> Bool
    func start() throws
    func finish() throws -> Data
    func cancel()
}

/// Permission may return after the user leaves Talk. Only the same active request
/// can open the microphone; finishing consumes the recording exactly once.
@Observable @MainActor final class VoiceInput {
    enum Phase { case idle, requesting, recording, ready }
    private(set) var phase: Phase = .idle
    private(set) var error: String?
    private let recorder: any VoiceRecording
    private var requestID = UUID()

    init(recorder: any VoiceRecording) {
        self.recorder = recorder
        recorder.onFinish = { [weak self] success in
            guard let self, self.phase == .recording else { return }
            if success { self.phase = .ready }
            else {
                self.cancel()
                self.error = "Recording was interrupted. Tap Talk to try again."
            }
        }
    }

    func start() async {
        guard phase == .idle else { return }
        let id = UUID()
        requestID = id
        phase = .requesting
        error = nil
        let allowed = await recorder.requestPermission()
        guard requestID == id else { return }
        guard !Task.isCancelled else { cancel(); return }
        guard allowed else {
            phase = .idle
            error = "Allow microphone access for Kyra in Settings > Privacy & Security > Microphone."
            return
        }
        do {
            try recorder.start()
            phase = .recording
        } catch {
            recorder.cancel()
            phase = .idle
            self.error = error.localizedDescription
        }
    }

    func finish() -> Data? {
        guard phase == .recording || phase == .ready else { return nil }
        phase = .idle
        do { return try recorder.finish() }
        catch {
            recorder.cancel()
            self.error = error.localizedDescription
            return nil
        }
    }

    func cancel() {
        requestID = UUID()
        recorder.cancel()
        phase = .idle
        error = nil
    }
}

#if os(visionOS)
@MainActor final class HeadsetVoiceRecorder: NSObject, VoiceRecording, AVAudioRecorderDelegate {
    var onFinish: ((Bool) -> Void)?
    private var sessionActive = false
    private var recorder: AVAudioRecorder?
    private var file: URL?

    func requestPermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    func start() throws {
        cancel()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default)
        try session.setActive(true)
        sessionActive = true
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("kyra-\(UUID()).wav")
        file = url
        let recording = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false
        ])
        recorder = recording
        recording.delegate = self
        guard recording.record(forDuration: 60) else {
            cancel()
            throw VoiceFailure("The microphone could not start. Try again.")
        }
    }

    func finish() throws -> Data {
        guard let recorder, let file else { throw VoiceFailure("No recording to send.") }
        recorder.stop()
        defer { cancel() }
        return try Data(contentsOf: file)
    }

    func cancel() {
        recorder?.stop()
        recorder = nil
        if let file { try? FileManager.default.removeItem(at: file) }
        file = nil
        if sessionActive {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            sessionActive = false
        }
    }

    nonisolated func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        Task { @MainActor [weak self] in
            guard let self, self.recorder === recorder else { return }
            self.onFinish?(flag)
        }
    }

    nonisolated func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: Error?) {
        Task { @MainActor [weak self] in
            guard let self, self.recorder === recorder else { return }
            self.onFinish?(false)
        }
    }
}
#endif
