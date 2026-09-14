import SwiftUI
import AVFoundation

struct Line: Identifiable {
    enum Who { case duc, kyra, system }
    let id = UUID()
    let who: Who
    var text: String
    var badge: String?
}

struct ContentView: View {
    let client: KyraClient
    @Environment(\.scenePhase) private var scenePhase

    @State private var lines: [Line] = []
    @State private var draft = ""
    @State private var presence: Presence = .idle
    @State private var backend = "…"
    @State private var showSettings = false
    @State private var turn: Task<Void, Never>?
    @State private var speech = SpeechPlayer()
    @State private var voice = VoiceInput(recorder: HeadsetVoiceRecorder())
    @State private var turnID = UUID()

    private var connected: Bool { !client.baseURL.isEmpty }
    private var answering: Bool { turn != nil }

    var body: some View {
        VStack(spacing: 0) {
            PresenceReadout(presence: presence)
                .padding(.top, 26)
                .padding(.bottom, 8)
                // Tapping her is how you cut her off - the mic button does the
                // same job on the web, and on a headset the orb is what you look at.
                .onTapGesture { if speech.isSpeaking { stop() } }
            transcript
            composer
        }
        .ornament(attachmentAnchor: .scene(.top)) {
            // The backend badge lives on an ornament rather than in the window,
            // so it never competes with the conversation for attention.
            Text(connected ? backend.uppercased() : "NOT CONNECTED")
                .font(.system(.caption, design: .monospaced))
                .padding(.horizontal, 16).padding(.vertical, 10)
                .glassBackgroundEffect()
                .onTapGesture { stop(); showSettings = true }
        }
        .sheet(isPresented: $showSettings) { settings }
        .task { await refreshBackend() }
        .onDisappear { stop() }
        .onChange(of: speech.error) {
            if let error = speech.error {
                lines.append(Line(who: .system, text: "Could not play the reply: \(error)"))
            }
        }
        .onChange(of: scenePhase) {
            if scenePhase == .background { stop() }
        }
        .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.interruptionNotification)) { note in
            if let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
               type == AVAudioSession.InterruptionType.began.rawValue { stop() }
        }
        .onChange(of: voice.phase) {
            if voice.phase == .ready { presence = .idle }
        }
        .onChange(of: voice.error) {
            if let error = voice.error {
                lines.append(Line(who: .system, text: error))
                presence = .failed
            }
        }
        .onAppear {
            if !connected { showSettings = true }
            // Lets a question be handed in at launch:
            //   xcrun simctl launch booted com.kyra.KyraVision --args -kyra.ask "hello"
            // which is how the client can be driven without a keyboard, and the
            // same seam a Shortcut or spatial Siri would use later.
            if let ask = UserDefaults.standard.string(forKey: "kyra.ask"), !ask.isEmpty, connected {
                draft = ask
                send()
            }
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    ForEach(lines) { line in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 8) {
                                Text(tag(for: line.who))
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                                if let badge = line.badge {
                                    Text(badge)
                                        .font(.system(.caption2, design: .monospaced))
                                        .padding(.horizontal, 7).padding(.vertical, 2)
                                        .background(.quaternary, in: Capsule())
                                }
                            }
                            Text(line.text)
                                // Body text, not the 13px mono the web HUD uses:
                                // a headset is further from the eye than a laptop.
                                .font(.system(.body))
                                .foregroundStyle(line.who == .system ? .secondary : .primary)
                                .textSelection(.enabled)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id(line.id)
                    }
                }
                .padding(28)
            }
            .onChange(of: lines.last?.text) {
                if let last = lines.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }

    private var composer: some View {
        VStack(spacing: 10) {
            HStack(spacing: 12) {
                if voice.phase != .idle {
                    Text(voice.phase == .recording ? "Listening… Tap Send voice when finished." :
                         voice.phase == .ready ? "One minute recorded. Send voice or Cancel." : "Waiting for microphone permission…")
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("Cancel") { voice.cancel(); presence = .idle }
                    if voice.phase == .recording || voice.phase == .ready {
                        Button("Send voice", systemImage: "arrow.up") { sendVoice() }
                            .buttonStyle(.borderedProminent)
                            .frame(minWidth: 120, minHeight: 60)
                    }
                } else {
                    TextField("Message Kyra", text: $draft, axis: .vertical)
                        .textFieldStyle(.plain)
                        .font(.system(.body))
                        .lineLimit(1...4)
                        .padding(.horizontal, 18).padding(.vertical, 14)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 22))
                        .onSubmit(send)
                    Button("Talk", systemImage: "mic.fill") { startRecording() }
                        .disabled(!connected || answering)
                        .frame(minWidth: 90, minHeight: 60)
                    Button(answering ? "Stop" : "Send") {
                        answering ? stop() : send()
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(answering ? .red : .accentColor)
                    .disabled(!answering && (draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !connected))
                    .frame(minWidth: 90, minHeight: 60)
                }
            }
        }
        .padding(20)
    }

    private func startRecording() {
        guard !answering, voice.phase == .idle, connected else { return }
        turnID = UUID()
        speech.stop()
        Task {
            await voice.start()
            if voice.phase == .recording { presence = .listening }
        }
    }

    private func sendVoice() {
        guard let wav = voice.finish() else {
            presence = .failed
            return
        }
        let id = UUID()
        turnID = id
        presence = .thinking
        turn = Task {
            defer { if turnID == id { turn = nil } }
            var heardSpeech = false
            do {
                try await client.sendVoice(wav) { event in
                    guard turnID == id, !Task.isCancelled else { return }
                    switch event {
                    case .transcript(let text):
                        heardSpeech = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        if heardSpeech { lines.append(Line(who: .duc, text: text)) }
                    case .audio(let clip):
                        presence = .speaking
                        speech.enqueue(clip)
                    case .done(let reply):
                        if !reply.reply.isEmpty {
                            lines.append(Line(who: .kyra, text: reply.reply, badge: reply.badge))
                        }
                    }
                }
                try Task.checkCancellation()
                if !heardSpeech {
                    lines.append(Line(who: .system, text: "I didn’t hear any words. Tap Talk and try again."))
                }
                while speech.isSpeaking {
                    try await Task.sleep(for: .milliseconds(120))
                }
                guard turnID == id else { return }
                presence = .idle
            } catch is CancellationError {
                // Stop discards queued clips and invalidates this turn.
            } catch {
                guard turnID == id else { return }
                speech.stop()
                lines.append(Line(who: .system, text: error.localizedDescription))
                presence = .failed
            }
        }
    }

    private var settings: some View {
        NavigationStack {
            Form {
                Section("Your Mac") {
                    TextField("http://192.168.1.20:8420", text: Binding(
                        get: { client.baseURL }, set: { client.baseURL = $0 }))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                }
                Section {
                    SecureField("KYRA_API_TOKEN", text: Binding(
                        get: { client.token }, set: { client.token = $0 }))
                } header: {
                    Text("Token")
                } footer: {
                    Text("Start the Mac with KYRA_HOST=0.0.0.0 and set KYRA_API_TOKEN in .env. "
                         + "Without the token the server refuses anything that is not the Mac itself.")
                }
            }
            .navigationTitle("Connect to Kyra")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        showSettings = false
                        Task { await refreshBackend() }
                    }
                }
            }
        }
        .frame(minWidth: 520, minHeight: 380)
    }

    private func tag(for who: Line.Who) -> String {
        switch who {
        case .duc: "YOU"
        case .kyra: "KYRA"
        case .system: "SYSTEM"
        }
    }

    private func refreshBackend() async {
        guard connected else { return }
        do {
            backend = try await client.backend()
        } catch {
            backend = "offline"
            presence = .failed
        }
    }

    private func send() {
        let message = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, connected, !answering, voice.phase == .idle else { return }
        let id = UUID()
        turnID = id
        draft = ""
        lines.append(Line(who: .duc, text: message))
        presence = .thinking

        turn = Task {
            defer { if turnID == id { turn = nil } }
            // One row is created on the first delta and grown in place, so the
            // reply appears as it is written rather than arriving whole.
            var replyIndex: Int?
            do {
                let out = try await client.send(message) { delta in
                    Task { @MainActor in
                        guard turnID == id else { return }
                        if let index = replyIndex, index < lines.count {
                            lines[index].text += delta
                        } else {
                            lines.append(Line(who: .kyra, text: delta))
                            replyIndex = lines.count - 1
                        }
                    }
                }
                try Task.checkCancellation()
                guard turnID == id else { return }
                if let index = replyIndex, index < lines.count {
                    lines[index].text = out.reply   // authoritative, e.g. a truncation marker
                    lines[index].badge = out.badge
                } else {
                    lines.append(Line(who: .kyra, text: out.reply, badge: out.badge))
                }
                presence = .idle
                await speakReply(out.reply, id: id)
            } catch is CancellationError {
                // stop() already set .interrupted.
            } catch {
                guard turnID == id else { return }
                lines.append(Line(who: .system, text: error.localizedDescription))
                presence = .failed
            }
        }
    }

    /// Reads the reply aloud, starting on the first sentence rather than waiting
    /// for the whole thing to be synthesised. Failing to speak is not failing the
    /// turn: the reply is already on screen, so a silent answer beats an error.
    private func speakReply(_ reply: String, id: UUID) async {
        guard !reply.isEmpty else { return }
        presence = .speaking
        do {
            try await client.speak(reply) { wav in
                Task { @MainActor in
                    guard turnID == id else { return }
                    speech.enqueue(wav)
                }
            }
            while speech.isSpeaking, turnID == id, presence == .speaking {
                try await Task.sleep(for: .milliseconds(120))
            }
        } catch {
            // fall through - she just does not say this one out loud
        }
        if turnID == id, presence == .speaking { presence = .idle }
    }

    private func stop() {
        let hadTurn = answering
        guard hadTurn || voice.phase != .idle || speech.isSpeaking else { return }
        turnID = UUID()
        voice.cancel()
        speech.stop()
        turn?.cancel()
        turn = nil
        // Cancelling the request only stops this end listening; the Mac keeps
        // generating and would file the whole reply into memory as if it had
        // been heard. /api/chat/cancel is the half that actually stops it.
        if hadTurn { Task { await client.cancel() } }
        if hadTurn, let last = lines.indices.last, lines[last].who == .kyra {
            lines[last].badge = "interrupted"
        }
        presence = .interrupted
    }
}
