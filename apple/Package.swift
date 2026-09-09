// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KyraQueueLab",
    platforms: [.macOS(.v14)],
    products: [.library(name: "KyraQueueLab", targets: ["KyraQueueLab"])],
    targets: [
        .target(name: "KyraQueueLab", path: "KyraVision/KyraVision",
                exclude: ["KyraVisionApp.swift", "KyraClient.swift", "ContentView.swift",
                          "PresenceOrb.swift", "SpeechPlayer.swift", "TodayView.swift",
                          "WorkspaceView.swift", "LearningLabView.swift"],
                sources: ["QueueSimulation.swift", "WorkspaceState.swift", "LearningLab.swift"]),
        .testTarget(name: "KyraQueueLabTests", dependencies: ["KyraQueueLab"], path: "Tests")
    ]
)
