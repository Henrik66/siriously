// swift-tools-version: 6.0
// siriously-overlay: quiet status HUD (PRD §4.6). No dependencies. Build:
//   swift build -c release --package-path mac/overlay
import PackageDescription

let package = Package(
    name: "SiriouslyOverlay",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "siriously-overlay"),
    ],
    swiftLanguageModes: [.v5]
)
