import XCTest
@testable import KyraQueueLab

@MainActor final class FakeWorkspaceAPI: WorkspaceAPI {
    var connection = KyraConnection(url: "http://mac-a", token: "test")
    var rows: [TaskCheckpoint] = []
    var failLoad = false
    var failSave: Error?
    var beforeSaveReply: (() -> Void)?
    var submissions: [TaskCheckpoint] = []
    func checkpoints() async throws -> [TaskCheckpoint] {
        if failLoad { throw URLError(.cannotConnectToHost) }
        return rows
    }
    func saveCheckpoint(_ checkpoint: TaskCheckpoint) async throws -> TaskCheckpoint {
        submissions.append(checkpoint)
        if let failSave { throw failSave }
        beforeSaveReply?()
        var saved = checkpoint
        saved.revision += 1
        return saved
    }
}

final class WorkspaceStateTests: XCTestCase {
    @MainActor func testFailedDestinationSwitchCannotReuseOldRows() async {
        let api = FakeWorkspaceAPI()
        var old = TaskCheckpoint()
        old.task = "Old Mac task"; old.nextAction = "Check result"; old.revision = 1
        api.rows = [old]
        let state = WorkspaceState(api: api)
        await state.load()
        state.select(old)
        api.connection.url = "http://mac-b"
        api.failLoad = true
        await state.connectionChanged()
        XCTAssertTrue(state.checkpoints.isEmpty)
        state.select(old)
        XCTAssertTrue(state.draft.task.isEmpty)
        XCTAssertEqual(api.submissions.count, 0)
    }

    @MainActor func testUncertainSaveRetriesFrozenPayloadOnOriginalConnection() async {
        let api = FakeWorkspaceAPI()
        let state = WorkspaceState(api: api)
        state.draft.task = "Test task"; state.draft.nextAction = "Test action"
        api.failSave = URLError(.networkConnectionLost)
        await state.save()
        XCTAssertTrue(state.retryPending)
        XCTAssertFalse(state.canEdit)
        api.connection.url = "http://mac-b"
        await state.save()
        XCTAssertEqual(api.submissions.count, 1)
        api.connection.url = "http://mac-a"
        api.failSave = nil
        await state.save()
        XCTAssertEqual(api.submissions[0], api.submissions[1])
        XCTAssertEqual(state.draft.revision, 1)
        XCTAssertFalse(state.dirty)
    }

    @MainActor func testValidationUnlocksDraftAndConflictCanBeKeptAsNew() async {
        let api = FakeWorkspaceAPI()
        let state = WorkspaceState(api: api)
        state.draft.task = "Task"; state.draft.nextAction = "Action"
        api.failSave = KyraError.rejected(422, "Too long")
        await state.save()
        XCTAssertTrue(state.canEdit)
        XCTAssertFalse(state.retryPending)
        api.failSave = KyraError.rejected(409, "Conflict")
        await state.save()
        XCTAssertTrue(state.conflict)
        let oldID = state.draft.id
        state.keepAsNew()
        XCTAssertNotEqual(state.draft.id, oldID)
        XCTAssertEqual(state.draft.task, "Task")
        XCTAssertFalse(state.conflict)
    }

    @MainActor func testLateAcknowledgementNeverReassignsOldMacRows() async {
        let api = FakeWorkspaceAPI()
        var old = TaskCheckpoint()
        old.task = "Mac A only"; old.nextAction = "Read"; old.revision = 1
        api.rows = [old]
        let state = WorkspaceState(api: api)
        await state.load()
        state.draft.task = "New A"; state.draft.nextAction = "Run"
        api.beforeSaveReply = { api.connection.url = "http://mac-b" }
        await state.save()
        api.beforeSaveReply = nil
        state.clear()
        state.draft.task = "New B"; state.draft.nextAction = "Run"
        await state.save()
        XCTAssertFalse(state.checkpoints.contains(old))
        state.select(old)
        XCTAssertEqual(state.draft.task, "New B")
    }

    @MainActor func testRejectedRetryDoesNotUnlockEarlierUncertainSave() async {
        let api = FakeWorkspaceAPI()
        let state = WorkspaceState(api: api)
        state.draft.task = "Task"; state.draft.nextAction = "Action"
        api.failSave = URLError(.networkConnectionLost)
        await state.save()
        api.failSave = KyraError.unauthorized
        await state.save()
        XCTAssertTrue(state.retryPending)
        XCTAssertFalse(state.canEdit)
    }
}
