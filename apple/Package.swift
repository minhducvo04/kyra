// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KyraQueueLab",
    platforms: [.macOS(.v14)],
    products: [.library(name: "KyraQueueLab", targets: ["KyraQueueLab"]),
               .library(name: "KyraCastProtocol", targets: ["KyraCastProtocol"])],
    targets: [
        .target(name: "KyraQueueLab", path: "KyraVision/KyraVision",
                exclude: ["Assets.xcassets", "KyraVisionApp.swift", "KyraClient.swift", "ContentView.swift",
                          "PresenceOrb.swift", "SpeechPlayer.swift", "TodayView.swift", "OwlVolumeView.swift", "CapybaraVolumeView.swift", "CompanionPresentation.swift", "PetPerchView.swift", "PetFollowView.swift", "PetPoseSource.swift", "PetActionDock.swift",
                          "WorkspaceView.swift", "LearningLabView.swift", "PanelWindowScene.swift", "CastWindowScene.swift", "CastSpace.swift", "CastArtifactScene.swift", "SketchSpaceView.swift", "SketchStrokeMesh.swift", "Model3DView.swift"],
                sources: ["QueueSimulation.swift", "WorkspaceState.swift", "LearningLab.swift",
                          "VoiceInput.swift", "VoiceStream.swift", "OrbPresentation.swift", "SecondOpinion.swift", "PanelWindows.swift", "OwlPose.swift", "SpaceGesture.swift", "OwlMotion.swift", "OwlStance.swift", "OwlGeometry.swift", "CapybaraGeometry.swift", "PetLayout.swift", "PetLanding.swift", "PetRolePolicy.swift", "PetLifecycle.swift", "PetPerch.swift", "PetFollow.swift", "PetDock.swift", "PetPlacementGuide.swift", "SpatialSketch.swift", "Model3D.swift"]),
        .target(name: "KyraCastProtocol", path: "KyraCast/Protocol"),
        .target(name: "KyraCastNative", dependencies: ["KyraCastProtocol"], path: "KyraCast/Shared"),
        .testTarget(name: "KyraQueueLabTests", dependencies: ["KyraQueueLab", "KyraCastProtocol", "KyraCastNative"], path: "Tests")
    ]
)
