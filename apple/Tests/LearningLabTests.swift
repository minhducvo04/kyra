import XCTest
@testable import KyraQueueLab

@MainActor final class FakeLessonAPI: LessonAPI {
    var connection = KyraConnection(url: "http://mac-a", token: "test")
    var failure: Error?
    var submissions: [(UUID, String, String)] = []
    func saveLesson(requestID: UUID, summary: String, takeaway: String) async throws -> LearningItem {
        submissions.append((requestID, summary, takeaway))
        if let failure { throw failure }
        return LearningItem(id: 1, topic: "Queue", summary: summary, keyTakeaway: takeaway)
    }
}

final class LearningLabTests: XCTestCase {
    @MainActor func testControlsCannotDiscardUnsavedTakeawayOrPendingRetry() async {
        let lab = LearningLab()
        lab.run()
        lab.takeaway = "My own lesson"
        let id = lab.requestID
        lab.toggle(0)
        lab.setArrivals(9)
        lab.run()
        XCTAssertEqual(lab.takeaway, "My own lesson")
        XCTAssertEqual(lab.servers, [true, true, true])
        XCTAssertEqual(lab.arrivals, 5)
        XCTAssertEqual(lab.requestID, id)
        lab.discardLesson()
        lab.toggle(0)
        XCTAssertEqual(lab.servers, [false, true, true])
    }

    @MainActor func testRetryCannotMoveBetweenMacsOrTokens() async {
        let api = FakeLessonAPI()
        let lab = LearningLab()
        lab.run()
        api.failure = URLError(.networkConnectionLost)
        await lab.save(using: api)
        api.connection.token = "different-test-token"
        await lab.save(using: api)
        XCTAssertEqual(api.submissions.count, 1)
        api.connection.token = "test"
        api.failure = nil
        await lab.save(using: api)
        XCTAssertEqual(api.submissions[0].0, api.submissions[1].0)
        XCTAssertEqual(api.submissions[0].2, api.submissions[1].2)
        XCTAssertTrue(lab.saved)
    }

    @MainActor func testDefinitiveRejectionAllowsCorrectionWithoutLosingText() async {
        let api = FakeLessonAPI()
        let lab = LearningLab()
        lab.run()
        lab.takeaway = "My lesson"
        api.failure = KyraError.rejected(422, "Invalid")
        await lab.save(using: api)
        XCTAssertFalse(lab.saveAttempted)
        XCTAssertEqual(lab.takeaway, "My lesson")
        lab.takeaway = "Corrected lesson"
        api.failure = nil
        await lab.save(using: api)
        XCTAssertTrue(lab.saved)
        XCTAssertEqual(api.submissions.last?.2, "Corrected lesson")
    }

    @MainActor func testRejectedRetryKeepsOriginalRequestIdentity() async {
        let api = FakeLessonAPI()
        let lab = LearningLab()
        lab.run()
        api.failure = URLError(.networkConnectionLost)
        await lab.save(using: api)
        let id = lab.requestID
        api.failure = KyraError.unauthorized
        await lab.save(using: api)
        XCTAssertEqual(lab.requestID, id)
        XCTAssertTrue(lab.saveAttempted)
        api.failure = nil
        await lab.save(using: api)
        XCTAssertEqual(Set(api.submissions.map { $0.0 }).count, 1)
    }

    @MainActor func testMalformedURLCanBeCorrectedBeforeAnyRequestWasSent() async {
        let api = FakeLessonAPI()
        let lab = LearningLab()
        lab.run()
        api.connection.url = "invalid"
        api.failure = KyraError.badURL
        await lab.save(using: api)
        XCTAssertFalse(lab.saveAttempted)
        api.connection.url = "http://mac-a"
        api.failure = nil
        await lab.save(using: api)
        XCTAssertTrue(lab.saved)
    }
}
