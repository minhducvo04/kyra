import AVFoundation
import Observation

/// Plays Kyra's reply as it arrives, one sentence at a time.
///
/// The server sends a separate complete WAV per sentence rather than one long
/// stream, so this is a queue and not a decoder: the first sentence starts
/// while the rest is still being synthesised, which is most of the difference
/// between a companion that answers and one you wait for (docs/voice-latency.md).
///
/// `stop()` drops what has not been said yet as well as what is playing. The
/// browser needed the same thing: clearing only the current clip means she
/// carries on with the next sentence a moment after being cut off, which reads
/// as not listening.
@Observable
@MainActor
final class SpeechPlayer: NSObject {
    private(set) var isSpeaking = false
    private(set) var playbackLevel: Double = 0
    private(set) var error: String?
    private var queue: [Data] = []
    private var player: AVAudioPlayer?
    private var metering: Task<Void, Never>?

    func enqueue(_ wav: Data) {
        error = nil
        queue.append(wav)
        if player == nil { playNext() }
    }

    func stop() {
        stopMetering()
        queue.removeAll()
        player?.stop()
        player = nil
        isSpeaking = false
    }

    private func playNext() {
        stopMetering()
        guard !queue.isEmpty else {
            player = nil
            isSpeaking = false
            return
        }
        let wav = queue.removeFirst()
        do {
            // Spoken audio supports playback; mixing keeps other apps audible.
            // Set this explicitly after microphone capture leaves the record category.
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: .mixWithOthers)
            try AVAudioSession.sharedInstance().setActive(true)
            let next = try AVAudioPlayer(data: wav)
            next.delegate = self
            next.isMeteringEnabled = true
            player = next
            isSpeaking = true
            guard next.play() else { throw VoiceFailure("Audio playback could not start.") }
            startMetering(next)
        } catch {
            self.error = error.localizedDescription
            player = nil
            playNext()
        }
    }

    private func stopMetering() {
        metering?.cancel()
        metering = nil
        playbackLevel = 0
    }

    private func startMetering(_ activePlayer: AVAudioPlayer) {
        metering = Task { @MainActor [weak self, weak activePlayer] in
            while !Task.isCancelled {
                guard let self, let activePlayer, self.player === activePlayer else { return }
                activePlayer.updateMeters()
                // Convert decibels to linear amplitude; silence stays still.
                let decibels = activePlayer.averagePower(forChannel: 0)
                self.playbackLevel = decibels <= -60 ? 0 : min(1, pow(10, Double(decibels) / 20))
                do { try await Task.sleep(for: .milliseconds(50)) }
                catch { return }
            }
        }
    }
}

extension SpeechPlayer: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor [weak self] in
            guard let self, self.player === player else { return }
            self.playNext()
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor [weak self] in
            guard let self, self.player === player else { return }
            self.playNext()
        }
    }
}
