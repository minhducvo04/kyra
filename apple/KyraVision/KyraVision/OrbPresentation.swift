import Foundation

/// One vocabulary for the orb, conversation card and controls.
enum Presence: String, CaseIterable {
    case idle, listening, thinking, speaking, interrupted, failed

    var label: String {
        switch self {
        case .idle: "STANDBY"
        case .listening: "LISTENING"
        case .thinking: "PROCESSING"
        case .speaking: "SPEAKING"
        case .interrupted: "INTERRUPTED"
        case .failed: "FAULT"
        }
    }
}

enum OrbControl: Hashable {
    case talk, text, stop
}

struct TranscriptLine: Equatable, Identifiable {
    let id = UUID()
    var who: String
    var text: String
    var badge: String?
}

struct OrbPresentation {
    var presence: Presence = .idle
    var cardHidden = false
    var reduceMotion = false
    var lines: [TranscriptLine] = []

    var visibleControls: [OrbControl] { [.talk, .text, .stop] }
    var stopEnabled: Bool { [.listening, .thinking, .speaking].contains(presence) }

    mutating func hideCard() { cardHidden = true }
    mutating func showCard() { cardHidden = false }

    func pulseAmplitude(level: Double) -> Double {
        guard !reduceMotion, presence == .speaking else { return 0 }
        return min(1, max(0, level))
    }
}
