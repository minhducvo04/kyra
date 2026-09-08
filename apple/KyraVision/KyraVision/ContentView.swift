import SwiftUI

struct Line: Identifiable {
    enum Who { case duc, kyra, system }
    let id = UUID()
    let who: Who
    var text: String
    var badge: String?
}

struct ContentView: View {
    let client: KyraClient

    @State private var lines: [Line] = []
    @State private var draft = ""
    @State private var presence: Presence = .idle
    @State private var backend = "…"
    @State private var showSettings = false
    @State private var turn: Task<Void, Never>?

    private var connected: Bool { !client.baseURL.isEmpty }
    private var thinking: Bool { presence == .thinking }

    var body: some View {
        VStack(spacing: 0) {
            PresenceReadout(presence: presence)
                .padding(.top, 26)
                .padding(.bottom, 8)
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
                .onTapGesture { showSettings = true }
        }
        .sheet(isPresented: $showSettings) { settings }
        .task { await refreshBackend() }
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
        HStack(spacing: 12) {
            TextField("Say something to Kyra", text: $draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(.body))
                .lineLimit(1...4)
                .padding(.horizontal, 18).padding(.vertical, 14)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 22))
                .onSubmit(send)

            // Send becomes Stop while she is answering, the same as the web HUD:
            // stopping is part of the conversation, not an error path.
            Button(thinking ? "Stop" : "Send") {
                thinking ? stop() : send()
            }
            .buttonStyle(.borderedProminent)
            .tint(thinking ? .red : .accentColor)
            .disabled(!thinking && (draft.isEmpty || !connected))
            // visionOS targets want to be comfortably large; eyes plus pinch is
            // a coarser pointer than a trackpad.
            .frame(minWidth: 96, minHeight: 60)
        }
        .padding(20)
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
        guard !message.isEmpty, connected else { return }
        draft = ""
        lines.append(Line(who: .duc, text: message))
        presence = .thinking

        turn = Task {
            // One row is created on the first delta and grown in place, so the
            // reply appears as it is written rather than arriving whole.
            var replyIndex: Int?
            do {
                let out = try await client.send(message) { delta in
                    Task { @MainActor in
                        if let index = replyIndex, index < lines.count {
                            lines[index].text += delta
                        } else {
                            lines.append(Line(who: .kyra, text: delta))
                            replyIndex = lines.count - 1
                        }
                    }
                }
                if let index = replyIndex, index < lines.count {
                    lines[index].text = out.reply   // authoritative, e.g. a truncation marker
                    lines[index].badge = out.badge
                } else {
                    lines.append(Line(who: .kyra, text: out.reply, badge: out.badge))
                }
                presence = .idle
            } catch is CancellationError {
                // stop() already set .interrupted.
            } catch {
                lines.append(Line(who: .system, text: error.localizedDescription))
                presence = .failed
            }
        }
    }

    private func stop() {
        turn?.cancel()
        // Cancelling the request only stops this end listening; the Mac keeps
        // generating and would file the whole reply into memory as if it had
        // been heard. /api/chat/cancel is the half that actually stops it.
        Task { await client.cancel() }
        if let last = lines.indices.last, lines[last].who == .kyra {
            lines[last].badge = "interrupted"
        }
        presence = .interrupted
    }
}
