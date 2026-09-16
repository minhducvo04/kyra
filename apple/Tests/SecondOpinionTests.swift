// Red until Codex builds V02: a cross-company second opinion asked from the conversation card.
// Contract, SecondOpinion.swift (package sources; pure, no networking):
//   struct LoopRunSummary: Decodable, Equatable { id: Int; status: String; developer: String; servedModel: String?; output: String? }
//       decodes GET /api/loop/runs/{id}: run.id, run.status, run.developer, run.served_model, artifact.output
//   enum SecondOpinion {
//       static func choice(forBadge badge: String?) -> String
//           // "claude" -> "codex-default" (the other company); anything else, including nil and "local" -> "claude-fable-high"
//       static func prompt(request: String, answer: String) -> String
//           // contains the request, the answer, its SHA-256 hex, and the words "review comment"; never asks for tools
//       static func line(for run: LoopRunSummary) -> TranscriptLine?
//           // done -> who "review", badge = run.developer, text = output; failed/mismatch/unreconciled -> who "system",
//           // text names the status; queued/dispatching -> nil (still pending)
//   }
import XCTest
@testable import KyraQueueLab

final class SecondOpinionTests: XCTestCase {
    func testTheOtherCompanyReviews() {
        XCTAssertEqual(SecondOpinion.choice(forBadge: "claude"), "codex-default")
        XCTAssertEqual(SecondOpinion.choice(forBadge: "local"), "claude-fable-high")
        XCTAssertEqual(SecondOpinion.choice(forBadge: nil), "claude-fable-high")
    }

    func testPromptCarriesRequestAnswerAndHash() {
        let prompt = SecondOpinion.prompt(request: "What is 2+2?", answer: "4")
        XCTAssertTrue(prompt.contains("What is 2+2?") && prompt.contains("\"4\""))
        // sha256("4")
        XCTAssertTrue(prompt.contains("4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb41b56ff2ce5a6c1f0"))
        XCTAssertTrue(prompt.lowercased().contains("review comment"))
        XCTAssertFalse(prompt.lowercased().contains("use tools"))
    }

    func testRunSummaryDecodesTheServerShape() throws {
        let json = """
        {"run": {"id": 7, "status": "done", "developer": "OpenAI", "served_model": null, "tier": "work"},
         "artifact": {"prompt": "p", "output": "Looks right."}, "reviews": [], "reconciliations": []}
        """.data(using: .utf8)!
        let run = try JSONDecoder().decode(LoopRunSummary.self, from: json)
        XCTAssertEqual(run, LoopRunSummary(id: 7, status: "done", developer: "OpenAI", servedModel: nil, output: "Looks right."))
    }

    func testLineReflectsTheRunState() {
        let done = LoopRunSummary(id: 1, status: "done", developer: "OpenAI", servedModel: "gpt-6-astra", output: "Fine.")
        let line = SecondOpinion.line(for: done)
        XCTAssertEqual(line?.who, "review"); XCTAssertEqual(line?.badge, "OpenAI"); XCTAssertEqual(line?.text, "Fine.")
        let failed = SecondOpinion.line(for: LoopRunSummary(id: 2, status: "failed", developer: "OpenAI", servedModel: nil, output: nil))
        XCTAssertEqual(failed?.who, "system"); XCTAssertTrue(failed?.text.contains("failed") ?? false)
        XCTAssertNil(SecondOpinion.line(for: LoopRunSummary(id: 3, status: "dispatching", developer: "OpenAI", servedModel: nil, output: nil)))
    }
}
