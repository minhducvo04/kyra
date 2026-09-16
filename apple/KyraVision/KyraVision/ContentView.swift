import SwiftUI
import AVFoundation

struct ContentView: View {
    let client: KyraClient
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var textFocused: Bool

    @State private var presentation = OrbPresentation()
    @State private var draft = ""
    @State private var backend = "…"
    @State private var showSettings = false
    @State private var turn: Task<Void, Never>?
    @State private var speech = SpeechPlayer()
    @State private var voice = VoiceInput(recorder: HeadsetVoiceRecorder())
    @State private var turnID = UUID()

    private var connected: Bool { !client.baseURL.isEmpty }
    private var answering: Bool { turn != nil }

    var body: some View {
        HStack(spacing: 16) {
            VStack(spacing: 18) {
                PresenceReadout(presentation: presentation, playbackLevel: speech.playbackLevel)
                    .onTapGesture { if speech.isSpeaking { stop() } }
                controls
            }
            .frame(width: 180)
            .frame(maxHeight: .infinity)

            if !presentation.cardHidden {
                VStack(spacing: 0) {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Conversation").font(.headline)
                            Text(presentation.presence.label)
                                .font(.system(.caption2, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            textFocused = false
                            presentation.hideCard()
                        } label: {
                            Image(systemName: "chevron.right")
                        }
                        .accessibilityLabel("Hide conversation")
                    }
                    .padding(16)
                    transcript
                    composer
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .glassBackgroundEffect(in: RoundedRectangle(cornerRadius: 28))
            }
        }
        .padding(20)
        .onChange(of: reduceMotion, initial: true) { presentation.reduceMotion = reduceMotion }
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
                presentation.lines.append(TranscriptLine(who: "system", text: "Could not play the reply: \(error)"))
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
            // A captured clip still belongs to the active listening interaction until sent or cancelled.
            if voice.phase == .ready { presentation.presence = .listening }
        }
        .onChange(of: voice.error) {
            if let error = voice.error {
                presentation.lines.append(TranscriptLine(who: "system", text: error))
                presentation.presence = .failed
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
                    ForEach(presentation.lines) { line in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 8) {
                                Text(line.who.uppercased())
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
                                .foregroundStyle(line.who == "system" ? .secondary : .primary)
                                .textSelection(.enabled)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id(line.id)
                    }
                }
                .padding(20)
            }
            .onAppear {
                if let last = presentation.lines.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
            .onChange(of: presentation.lines.last?.text) {
                if let last = presentation.lines.last {
                    if reduceMotion { proxy.scrollTo(last.id, anchor: .bottom) }
                    else { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
                }
            }
        }
    }

    // This rail stays mounted when the card is hidden, including its Stop button.
    private var controls: some View {
        VStack(spacing: 12) {
            ForEach(presentation.visibleControls, id: \.self) { control in
                switch control {
                case .talk:
                    Button("Talk", systemImage: "mic.fill") {
                        presentation.showCard()
                        textFocused = false
                        startRecording()
                    }
                    .disabled(!connected || answering)
                case .text:
                    Button("Text", systemImage: "text.bubble") {
                        if voice.phase != .idle {
                            voice.cancel()
                            presentation.presence = .idle
                        }
                        presentation.showCard()
                        textFocused = true
                    }
                case .stop:
                    Button("Stop", systemImage: "stop.fill", action: stop)
                        .tint(.red)
                        .disabled(!presentation.stopEnabled)
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
    }

    private var composer: some View {
        VStack(spacing: 12) {
            if voice.phase != .idle {
                Text(voice.phase == .recording ? "Listening… Tap Send voice when finished." :
                     voice.phase == .ready ? "One minute recorded. Send voice or Cancel." : "Waiting for microphone permission…")
                    .frame(maxWidth: .infinity, alignment: .leading)
                HStack {
                    Button("Cancel") { voice.cancel(); presentation.presence = .idle }
                    if voice.phase == .recording || voice.phase == .ready {
                        Button("Send voice", systemImage: "arrow.up") { sendVoice() }
                            .buttonStyle(.borderedProminent)
                    }
                }
            } else {
                TextField("Message Kyra", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(.body))
                    .lineLimit(1...4)
                    .padding(.horizontal, 18).padding(.vertical, 14)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 22))
                    .focused($textFocused)
                    .onSubmit(send)
                Button("Send", systemImage: "arrow.up", action: send)
                    .buttonStyle(.borderedProminent)
                    .disabled(answering || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !connected)
            }
        }
        .padding(16)
    }

    private func startRecording() {
        guard !answering, voice.phase == .idle, connected else { return }
        let id = UUID()
        turnID = id
        speech.stop()
        presentation.presence = .listening
        Task {
            guard turnID == id else { return }
            await voice.start()
            guard turnID == id else { return }
            if voice.phase == .recording { presentation.presence = .listening }
        }
    }

    private func sendVoice() {
        guard let wav = voice.finish() else {
            presentation.presence = .failed
            return
        }
        let id = UUID()
        turnID = id
        presentation.presence = .thinking
        turn = Task {
            defer { if turnID == id { turn = nil } }
            var heardSpeech = false
            do {
                try await client.sendVoice(wav) { event in
                    guard turnID == id, !Task.isCancelled else { return }
                    switch event {
                    case .transcript(let text):
                        heardSpeech = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        if heardSpeech { presentation.lines.append(TranscriptLine(who: "you", text: text)) }
                    case .audio(let clip):
                        presentation.presence = .speaking
                        speech.enqueue(clip)
                    case .done(let reply):
                        if !reply.reply.isEmpty {
                            presentation.lines.append(TranscriptLine(who: "kyra", text: reply.reply, badge: reply.badge))
                        }
                    }
                }
                try Task.checkCancellation()
                if !heardSpeech {
                    presentation.lines.append(TranscriptLine(who: "system", text: "I didn’t hear any words. Tap Talk and try again."))
                }
                while speech.isSpeaking {
                    try await Task.sleep(for: .milliseconds(120))
                }
                guard turnID == id else { return }
                presentation.presence = .idle
            } catch is CancellationError {
                // Stop discards queued clips and invalidates this turn.
            } catch {
                guard turnID == id else { return }
                speech.stop()
                presentation.lines.append(TranscriptLine(who: "system", text: error.localizedDescription))
                presentation.presence = .failed
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

    private func refreshBackend() async {
        guard connected else { return }
        do {
            backend = try await client.backend()
        } catch {
            backend = "offline"
            if !answering, voice.phase == .idle { presentation.presence = .failed }
        }
    }

    private func send() {
        let message = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, connected, !answering, voice.phase == .idle else { return }
        let id = UUID()
        turnID = id
        draft = ""
        presentation.lines.append(TranscriptLine(who: "you", text: message))
        presentation.presence = .thinking

        turn = Task {
            defer { if turnID == id { turn = nil } }
            // One row is created on the first delta and grown in place, so the
            // reply appears as it is written rather than arriving whole.
            var replyIndex: Int?
            do {
                let out = try await client.send(message) { delta in
                    Task { @MainActor in
                        guard turnID == id else { return }
                        if let index = replyIndex, index < presentation.lines.count {
                            presentation.lines[index].text += delta
                        } else {
                            presentation.lines.append(TranscriptLine(who: "kyra", text: delta))
                            replyIndex = presentation.lines.count - 1
                        }
                    }
                }
                try Task.checkCancellation()
                guard turnID == id else { return }
                if let index = replyIndex, index < presentation.lines.count {
                    presentation.lines[index].text = out.reply   // authoritative, e.g. a truncation marker
                    presentation.lines[index].badge = out.badge
                } else {
                    presentation.lines.append(TranscriptLine(who: "kyra", text: out.reply, badge: out.badge))
                }
                presentation.presence = .idle
                await speakReply(out.reply, id: id)
            } catch is CancellationError {
                // stop() already set .interrupted.
            } catch {
                guard turnID == id else { return }
                presentation.lines.append(TranscriptLine(who: "system", text: error.localizedDescription))
                presentation.presence = .failed
            }
        }
    }

    /// Reads the reply aloud, starting on the first sentence rather than waiting
    /// for the whole thing to be synthesised. Failing to speak is not failing the
    /// turn: the reply is already on screen, so a silent answer beats an error.
    private func speakReply(_ reply: String, id: UUID) async {
        guard !reply.isEmpty else { return }
        presentation.presence = .speaking
        do {
            try await client.speak(reply) { wav in
                Task { @MainActor in
                    guard turnID == id else { return }
                    speech.enqueue(wav)
                }
            }
            while speech.isSpeaking, turnID == id, presentation.presence == .speaking {
                try await Task.sleep(for: .milliseconds(120))
            }
        } catch {
            // fall through - she just does not say this one out loud
        }
        if turnID == id, presentation.presence == .speaking { presentation.presence = .idle }
    }

    private func stop() {
        let hadTurn = answering
        guard hadTurn || voice.phase != .idle || speech.isSpeaking || presentation.stopEnabled else { return }
        turnID = UUID()
        voice.cancel()
        speech.stop()
        turn?.cancel()
        turn = nil
        // Cancelling the request only stops this end listening; the Mac keeps
        // generating and would file the whole reply into memory as if it had
        // been heard. /api/chat/cancel is the half that actually stops it.
        if hadTurn { Task { await client.cancel() } }
        if hadTurn, let last = presentation.lines.indices.last, presentation.lines[last].who == "kyra" {
            presentation.lines[last].badge = "interrupted"
        }
        presentation.presence = .interrupted
    }
}
