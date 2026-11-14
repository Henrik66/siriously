// swift-tools-version: 6.0
// siriously-ear: the speech daemon (mic → VAD → WhisperKit small.en + SpeakerKit voice lock → POST /utterance).
// Depends on the local clone at ~/hackathon/argmax-oss-swift (two levels up). Build:
//   swift build -c release --package-path mac/ear
import PackageDescription

let package = Package(
    name: "SiriouslyEar",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(path: "../../argmax-oss-swift"),
    ],
    targets: [
        .executableTarget(
            name: "siriously-ear",
            dependencies: [
                .product(name: "WhisperKit", package: "argmax-oss-swift"),
                .product(name: "SpeakerKit", package: "argmax-oss-swift"),
            ]
        ),
    ],
    swiftLanguageModes: [.v5]
)
