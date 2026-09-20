import Foundation

protocol QueueSimulation {
    func run(arrivals: Int, servers: [Bool]) throws -> QueueRun
}

struct QueueTick: Equatable {
    let second: Int
    let arrived: Int
    let completed: Int
    let waiting: Int
    let byServer: [Int]
}

struct QueueRun: Equatable {
    let arrivals: Int
    let servers: [Bool]
    let ticks: [QueueTick]

    var final: QueueTick { ticks[ticks.count - 1] }
    var capacity: Int { servers.filter { $0 }.count * 2 }
    var explanation: String {
        let outcome = arrivals > capacity
            ? "Demand exceeded capacity by \(arrivals - capacity) requests each second, so the waiting queue grew."
            : "Capacity covered every arrival, so no requests were left waiting at the end of a tick."
        return "\(final.arrived) requests arrived in 10 seconds. \(final.completed) completed and \(final.waiting) remained waiting. "
            + "\(servers.filter { $0 }.count) active servers could complete \(capacity) requests per second. " + outcome
    }
}

/// A shared FIFO backlog; arrivals precede service each second. Servers take
/// work in numbered order, each completing at most two requests per tick.
/// There is no in-flight work between ticks, dropped work or wall-clock input.
struct SharedQueueSimulation: QueueSimulation {
    enum Invalid: Error { case configuration }

    func run(arrivals: Int, servers: [Bool]) throws -> QueueRun {
        guard (0...10).contains(arrivals), servers.count == 3 else { throw Invalid.configuration }
        var waiting = 0
        var completed = [0, 0, 0]
        var ticks: [QueueTick] = []
        for second in 1...10 {
            waiting += arrivals
            for server in 0..<3 where servers[server] {
                let served = min(2, waiting)
                waiting -= served
                completed[server] += served
            }
            ticks.append(QueueTick(second: second, arrived: arrivals * second,
                                   completed: completed.reduce(0, +), waiting: waiting, byServer: completed))
        }
        return QueueRun(arrivals: arrivals, servers: servers, ticks: ticks)
    }
}
