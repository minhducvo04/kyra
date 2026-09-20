import SwiftUI

extension Presence {
    var detail: String {
        switch self {
        case .idle: "awaiting input"
        case .listening: "go ahead"
        case .thinking: "querying model…"
        case .speaking: "tap to interrupt"
        case .interrupted: "tap Talk or Text to continue"
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
    let presentation: OrbPresentation
    var playbackLevel: Double = 0

    private var presence: Presence { presentation.presence }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0,
                                paused: presentation.reduceMotion || presence != .idle)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let breath = presentation.reduceMotion || presence != .idle
                ? 0 : (sin(t * .pi / 3) + 1) / 2
            let pulse = presentation.pulseAmplitude(level: playbackLevel)

            ZStack {
                Circle()
                    .fill(presence.tint.opacity(0.12))
                    .frame(width: 144, height: 144)
                    .blur(radius: 12)

                Circle()
                    .fill(RadialGradient(colors: [.white.opacity(0.95), presence.tint,
                                                  presence.tint.opacity(0.3), .black.opacity(0.75)],
                                         center: .init(x: 0.32, y: 0.24),
                                         startRadius: 0, endRadius: 130))
                    .overlay {
                        Circle().strokeBorder(.white.opacity(0.3), lineWidth: 1)
                    }
                    .frame(width: 112, height: 112)

                Ellipse()
                    .trim(from: 0.08, to: 0.9)
                    .stroke(presence.tint.opacity(0.7), style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .frame(width: 152, height: 58)
                    .rotationEffect(.degrees(-24))
            }
            .scaleEffect(1 + 0.035 * breath + 0.12 * pulse)
            .shadow(color: presence.tint.opacity(presence.glow), radius: 18 + 10 * pulse)
            .frame(width: 180, height: 180)
        }
        .accessibilityLabel("Kyra is \(presence.rawValue)")
    }
}

struct PresenceReadout: View {
    let presentation: OrbPresentation
    var playbackLevel: Double = 0

    private var presence: Presence { presentation.presence }

    var body: some View {
        VStack(spacing: 10) {
            PresenceOrb(presentation: presentation, playbackLevel: playbackLevel)
            Text(presence.label)
                .font(.system(.caption, design: .monospaced))
                .tracking(2)
                .foregroundStyle(presence == .failed ? Color.red : .primary)
            Text(presence.detail)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if presence == .speaking {
                Text("Voice \(Int(min(1, max(0, playbackLevel)) * 100))%")
                    .font(.system(.caption2, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }
}
