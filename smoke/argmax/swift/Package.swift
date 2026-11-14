// swift-tools-version: 6.0
// Throwaway Argmax OSS smoke tests (not app code).
//   speaker-embed : are SpeakerKit centroid embeddings stable across independent runs?
//   tts-smoke     : TTSKit time-to-first-audio / total time, with models under ~/hackathon/models
// Depends on the local clone at ~/hackathon/argmax-oss-swift (three levels up).
import PackageDescription

let package = Package(
    name: "ArgmaxSmoke",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(path: "../../../argmax-oss-swift"),
    ],
    targets: [
        .executableTarget(
            name: "speaker-embed",
            dependencies: [
                .product(name: "SpeakerKit", package: "argmax-oss-swift"),
                .product(name: "WhisperKit", package: "argmax-oss-swift"),
            ]
        ),
        .executableTarget(
            name: "tts-smoke",
            dependencies: [
                .product(name: "TTSKit", package: "argmax-oss-swift"),
            ]
        ),
    ],
    swiftLanguageModes: [.v5]
)
