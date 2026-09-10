import XCTest
@testable import KyraQueueLab

final class QueueSimulationTests: XCTestCase {
    func testEveryServerCombinationConservesWorkAtEveryTick() throws {
        for mask in 0..<8 {
            let servers = (0..<3).map { mask & (1 << $0) != 0 }
            for arrivals in 0...10 {
                let run = try SharedQueueSimulation().run(arrivals: arrivals, servers: servers)
                XCTAssertEqual(run.ticks.count, 10)
                let capacity = servers.filter { $0 }.count * 2
                for tick in run.ticks {
                    XCTAssertEqual(tick.arrived, arrivals * tick.second)
                    XCTAssertEqual(tick.arrived, tick.completed + tick.waiting)
                    XCTAssertEqual(tick.waiting, max(0, arrivals - capacity) * tick.second)
                    XCTAssertEqual(tick.byServer.reduce(0, +), tick.completed)
                    for i in 0..<3 {
                        XCTAssertLessThanOrEqual(tick.byServer[i], tick.second * 2)
                        if !servers[i] { XCTAssertEqual(tick.byServer[i], 0) }
                    }
                }
            }
        }
    }

    func testStableRoutingAndReplay() throws {
        let engine = SharedQueueSimulation()
        let first = try engine.run(arrivals: 3, servers: [true, true, true])
        XCTAssertEqual(first, try engine.run(arrivals: 3, servers: [true, true, true]))
        XCTAssertEqual(first.ticks.last?.byServer, [20, 10, 0])
        XCTAssertEqual(first.ticks.last?.waiting, 0)
        let disabled = try engine.run(arrivals: 5, servers: [false, false, false])
        XCTAssertEqual(disabled.ticks.last?.waiting, 50)
        XCTAssertEqual(disabled.ticks.last?.completed, 0)
    }

    func testRejectsUnboundedOrMalformedConfigurations() {
        for rate in [-1, 11, Int.max] {
            XCTAssertThrowsError(try SharedQueueSimulation().run(arrivals: rate, servers: [true, true, true]))
        }
        for servers in [[], [true], [true, true, true, true]] {
            XCTAssertThrowsError(try SharedQueueSimulation().run(arrivals: 2, servers: servers))
        }
    }
}
