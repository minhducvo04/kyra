import SwiftUI

/// Daily decisions: reminders, recall, and sourced suggestions to accept or dismiss.
///
/// The digest already assembles this at 05:00 on the Mac and writes a page Duc
/// reads in a browser. This is the part he acts on rather than reads:
/// reminders he can tick off, reviews he can answer, and proposed first steps. These are the
/// ones worth having in front of him while he is wearing it, and everything
/// else in the digest is reading matter.
///
/// Deliberately not here: profile, documents, autofill, the tracker. Those are
/// desk work, and putting them on a headset would be interface for its own sake.
struct TodayView: View {
    let client: KyraClient

    @State private var reminders: [Reminder] = []
    @State private var reviews: [LearningItem] = []
    @State private var initiatives: [InitiativeProposal] = []
    @State private var reasons: [String: String] = [:]
    @State private var pending: Set<String> = []
    @State private var suggestionsUnavailable = false
    @State private var loading = true
    @State private var problem: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.secondary)
                } else if loading {
                    ProgressView().frame(maxWidth: .infinity)
                } else if reminders.isEmpty && reviews.isEmpty && initiatives.isEmpty {
                    // Not an error state, and worth saying warmly rather than as a blank.
                    VStack(spacing: 8) {
                        Image(systemName: "checkmark.circle").font(.system(size: 42)).foregroundStyle(.tertiary)
                        Text("Nothing due. Go and do the interesting thing.")
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
                }

                HStack {
                    if suggestionsUnavailable {
                        Text("Suggestions need an updated server.").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Refresh", systemImage: "arrow.clockwise") { Task { await load() } }
                        .disabled(loading || !pending.isEmpty)
                }

                if !initiatives.isEmpty {
                    section("Kyra suggests", count: initiatives.count) {
                        Text("Choose a first step to add as an undated reminder.")
                            .font(.callout).foregroundStyle(.secondary)
                        ForEach(initiatives) { item in
                            VStack(alignment: .leading, spacing: 12) {
                                Text(item.title).font(.headline)
                                Text(item.firstStep)
                                Text("\(item.why) About \(item.minutes) minutes.")
                                    .font(.callout).foregroundStyle(.secondary)
                                DisclosureGroup("Evidence") {
                                    ForEach(item.evidence) { evidence in
                                        VStack(alignment: .leading, spacing: 4) {
                                            Text("\(evidence.source) · \(evidence.when)").font(.caption).foregroundStyle(.secondary)
                                            Text(evidence.quote).font(.callout)
                                        }.padding(.vertical, 6)
                                    }
                                }
                                if item.status == "proposed" {
                                    TextField("Dismiss reason (optional)", text: Binding(
                                        get: { reasons[item.id] ?? "" },
                                        set: { reasons[item.id] = String($0.prefix(2000)) }
                                    )).textFieldStyle(.roundedBorder)
                                }
                                HStack {
                                    Button(item.status == "accepting" ? "Finish adding reminder" : "Add reminder") {
                                        decide(item, accept: true)
                                    }.buttonStyle(.borderedProminent)
                                    if item.status == "proposed" {
                                        Button("Dismiss") { decide(item, accept: false) }
                                    }
                                    if pending.contains(item.id) { ProgressView() }
                                }.frame(minHeight: 60)
                            }
                            .disabled(pending.contains(item.id))
                            .padding(.bottom, 14)
                        }
                    }
                }

                if !reminders.isEmpty {
                    section("Due", count: reminders.count) {
                        ForEach(reminders) { reminder in
                            HStack(alignment: .firstTextBaseline, spacing: 14) {
                                Button {
                                    complete(reminder)
                                } label: {
                                    Image(systemName: "circle").font(.system(size: 20))
                                }
                                .buttonStyle(.plain)
                                .frame(minWidth: 60, minHeight: 60)
                                .accessibilityLabel("Complete: \(reminder.text)")

                                VStack(alignment: .leading, spacing: 3) {
                                    Text(reminder.text)
                                    if let day = reminder.day {
                                        Text(day).font(.caption).foregroundStyle(.tertiary)
                                    }
                                }
                                Spacer()
                            }
                        }
                    }
                }

                if !reviews.isEmpty {
                    section("To recall", count: reviews.count) {
                        ForEach(reviews) { item in
                            VStack(alignment: .leading, spacing: 10) {
                                Text(item.topic).font(.headline)
                                // The summary is hidden behind a disclosure on purpose: a
                                // review you can read the answer to is not a review.
                                DisclosureGroup("Show what you saved") {
                                    Text(item.summary)
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                        .padding(.top, 6)
                                }
                                HStack(spacing: 12) {
                                    Button("Remembered") { review(item, remembered: true) }
                                        .buttonStyle(.borderedProminent)
                                    Button("Forgot") { review(item, remembered: false) }
                                }
                                .frame(minHeight: 60)
                            }
                            .padding(.bottom, 10)
                        }
                    }
                }
            }
            .padding(34)
        }
        .task { await load() }
    }

    @ViewBuilder
    private func section(_ title: String, count: Int, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("\(title.uppercased())  \(count)")
                .font(.system(.caption, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.tertiary)
            content()
        }
    }

    private func load() async {
        loading = true
        problem = nil
        do {
            async let due = client.reminders()
            async let recall = client.dueReviews()
            reminders = try await due.filter { !$0.done }
            reviews = try await recall
        } catch {
            problem = error.localizedDescription
        }
        do {
            initiatives = try await client.initiatives()
            suggestionsUnavailable = false
        } catch KyraError.server(404) {
            initiatives = []
            suggestionsUnavailable = true
        } catch {
            problem = error.localizedDescription
        }
        loading = false
    }

    private func decide(_ item: InitiativeProposal, accept: Bool) {
        guard pending.insert(item.id).inserted else { return }
        Task {
            defer { pending.remove(item.id) }
            do {
                if accept {
                    _ = try await client.acceptInitiative(item.id)
                } else {
                    let reason = reasons[item.id]?.trimmingCharacters(in: .whitespacesAndNewlines)
                    _ = try await client.dismissInitiative(item.id, reason: reason?.isEmpty == false ? reason : nil)
                }
                initiatives.removeAll { $0.id == item.id }
                reasons.removeValue(forKey: item.id)
                reminders = try await client.reminders().filter { !$0.done }
                problem = nil
            } catch {
                // Keep the row: retry recovers the receipt after a lost response.
                problem = error.localizedDescription
            }
        }
    }

    private func complete(_ reminder: Reminder) {
        // Removed immediately rather than after a refetch: on a headset a tap that
        // does nothing for half a second reads as a tap that missed.
        reminders.removeAll { $0.id == reminder.id }
        Task {
            do { try await client.completeReminder(reminder.id) } catch { await load() }
        }
    }

    private func review(_ item: LearningItem, remembered: Bool) {
        reviews.removeAll { $0.id == item.id }
        Task {
            do { try await client.markReviewed(item.id, remembered: remembered) } catch { await load() }
        }
    }
}
