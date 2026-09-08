import SwiftUI

/// What Kyra is doing, as one value.
///
/// A companion's first job is to show its state - idle, listening, thinking,
/// speaking, interrupted, failed (docs/plans/2026-09-07-human-interface.md,
/// point 1). The web HUD learned this the hard way: before it had one state
/// machine, the core ring knew two states and the mic button five, set from a
/// dozen scattered assignments. Same vocabulary here, so the two front doors
/// describe her the same way.
enum Presence: String {
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

    var detail: String {
        switch self {
        case .idle: "awaiting input"
        case .listening: "go ahead"
        case .thinking: "querying model…"
        case .speaking: "tap to interrupt"
        case .interrupted: "listening again"
        case .failed: "check the connection"
        }
    }

    var tint: Color {
        switch self {
        case .failed: .red
        case .interrupted: .secondary
        default: .cyan
        }
    }

    /// Seconds per revolution of the outer ring. Thinking is the only state
    /// that should feel busy; the rest are meant to be ignorable.
    var period: Double {
        switch self {
        case .thinking: 6
        case .speaking: 14
        case .listening: 20
        default: 90
        }
    }

    var glow: Double {
        switch self {
        case .idle: 0.35
        case .interrupted: 0.2
        case .failed: 0.8
        default: 0.75
        }
    }
}

/// An abstract presence, not a face.
///
/// The research is explicit that an abstract shape reads as calmer and more
/// trustworthy than a face that can be slightly wrong, and the rings on the web
/// HUD are the reference. Drawn with plain SwiftUI shapes rather than
/// RealityKit: this lives in a window, and a particle system belongs in a
/// volume later, if it earns one.
struct PresenceOrb: View {
    let presence: Presence
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let turn = reduceMotion ? 0 : (t / presence.period).truncatingRemainder(dividingBy: 1) * 360
            // One breath every few seconds, faster while she is thinking.
            let breathe = reduceMotion ? 0.5
                : (sin(t * (presence == .thinking ? 3.0 : 1.2)) + 1) / 2

            ZStack {
                Circle()
                    .stroke(presence.tint.opacity(0.25), lineWidth: 1)
                    .frame(width: 148, height: 148)

                Circle()
                    .trim(from: 0, to: 0.62)
                    .stroke(presence.tint.opacity(0.8), style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .frame(width: 120, height: 120)
                    .rotationEffect(.degrees(turn))

                Circle()
                    .trim(from: 0, to: 0.28)
                    .stroke(.white.opacity(0.5), style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                    .frame(width: 92, height: 92)
                    .rotationEffect(.degrees(-turn * 1.7))

                Circle()
                    .fill(presence.tint.opacity(0.25 + 0.35 * breathe))
                    .frame(width: 26 + 8 * breathe, height: 26 + 8 * breathe)
                    .blur(radius: 6)

                Circle()
                    .fill(presence.tint)
                    .frame(width: 9, height: 9)
                    .opacity(0.6 + 0.4 * breathe)
            }
            .shadow(color: presence.tint.opacity(presence.glow), radius: 22)
            .frame(width: 160, height: 160)
        }
        .accessibilityLabel("Kyra is \(presence.rawValue)")
    }
}

struct PresenceReadout: View {
    let presence: Presence

    var body: some View {
        VStack(spacing: 10) {
            PresenceOrb(presence: presence)
            Text(presence.label)
                .font(.system(.caption, design: .monospaced))
                .tracking(2)
                .foregroundStyle(presence == .failed ? Color.red : .primary)
            Text(presence.detail)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }
}
