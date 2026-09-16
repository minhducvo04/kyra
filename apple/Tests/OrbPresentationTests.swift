// Red until Codex builds the native orb and card (docs/plans/2026-09-15-vision-orb-interface.md, "Review").
// Contract, OrbPresentation.swift (added to the package sources; Presence moves here from PresenceOrb.swift):
//   enum Presence: String, CaseIterable { idle, listening, thinking, speaking, interrupted, failed; var label: String }
//   enum OrbControl { case talk, text, stop }
//   struct TranscriptLine: Equatable { var who: String; var text: String }
//   struct OrbPresentation {
//       var presence: Presence = .idle
//       var cardHidden = false
//       var reduceMotion = false
//       var lines: [TranscriptLine] = []
//       var visibleControls: [OrbControl]        // always contains .talk, .text, .stop, in any state, card hidden or not
//       var stopEnabled: Bool                    // true while listening, thinking or speaking
//       mutating func hideCard() / showCard()    // never touches lines
//       func pulseAmplitude(level: Double) -> Double
//           // reduceMotion -> 0; speaking -> level clamped to 0...1; any other presence -> 0
//   }
import XCTest
@testable import KyraQueueLab

final class OrbPresentationTests: XCTestCase {
    func testStopIsVisibleInEveryStateEvenWithTheCardHidden() {
        for presence in Presence.allCases {
            for hidden in [false, true] {
                var orb = OrbPresentation(presence: presence)
                if hidden { orb.hideCard() }
                XCTAssertTrue(orb.visibleControls.contains(.stop), "\(presence) hidden=\(hidden)")
                XCTAssertTrue(orb.visibleControls.contains(.talk) && orb.visibleControls.contains(.text))
            }
        }
        XCTAssertEqual(Presence.allCases.filter { OrbPresentation(presence: $0).stopEnabled },
                       [.listening, .thinking, .speaking])
    }

    func testHidingTheCardKeepsTheTranscript() {
        var orb = OrbPresentation()
        orb.lines = [TranscriptLine(who: "you", text: "hello"), TranscriptLine(who: "kyra", text: "hi")]
        orb.hideCard()
        XCTAssertTrue(orb.cardHidden)
        XCTAssertEqual(orb.lines.count, 2)
        orb.showCard()
        XCTAssertFalse(orb.cardHidden)
        XCTAssertEqual(orb.lines.last?.text, "hi")
    }

    func testReducedMotionStopsThePulseEntirely() {
        var orb = OrbPresentation(presence: .speaking)
        orb.reduceMotion = true
        XCTAssertEqual(orb.pulseAmplitude(level: 0.9), 0)
        XCTAssertEqual(orb.presence.label, "SPEAKING")  // the label still reports what she is doing
    }

    func testPulseFollowsPlaybackLevelOnlyWhileSpeaking() {
        let speaking = OrbPresentation(presence: .speaking)
        XCTAssertEqual(speaking.pulseAmplitude(level: 0.4), 0.4, accuracy: 0.0001)
        XCTAssertEqual(speaking.pulseAmplitude(level: 7), 1)
        XCTAssertEqual(speaking.pulseAmplitude(level: -1), 0)
        for presence in Presence.allCases where presence != .speaking {
            XCTAssertEqual(OrbPresentation(presence: presence).pulseAmplitude(level: 0.9), 0, "\(presence)")
        }
    }

    func testTheSixWordsMatchTheWebHud() {
        XCTAssertEqual(Presence.allCases.map(\.label),
                       ["STANDBY", "LISTENING", "PROCESSING", "SPEAKING", "INTERRUPTED", "FAULT"])
    }
}
