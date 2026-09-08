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
    private var queue: [Data] = []
    private var player: AVAudioPlayer?

    func enqueue(_ wav: Data) {
        queue.append(wav)
        if player == nil { playNext() }
    }

    func stop() {
        queue.removeAll()
        player?.stop()
        player = nil
        isSpeaking = false
    }

    private func playNext() {
        guard !queue.isEmpty else {
            player = nil
            isSpeaking = false
            return
        }
        let wav = queue.removeFirst()
        do {
            // Ambient rather than playback: on a headset Kyra is one thing in the
            // room, not the thing you stopped everything else for.
            try? AVAudioSession.sharedInstance().setCategory(.ambient, mode: .spokenAudio)
            try? AVAudioSession.sharedInstance().setActive(true)
            let next = try AVAudioPlayer(data: wav)
            next.delegate = self
            player = next
            isSpeaking = true
            next.play()
        } catch {
            // One unplayable clip should not silence the rest of the sentence.
            playNext()
        }
    }
}

extension SpeechPlayer: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor [weak self] in self?.playNext() }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor [weak self] in self?.playNext() }
    }
}
