// TTSKit smoke test: generate a sentence, play it, report time-to-first-audio and total time.
// Models are downloaded to --models (external drive), never to ~/Documents/huggingface.
//
// Usage: tts-smoke --models <dir> --tokenizer <dir with tokenizer.json> [--text "..."] [--out <dir>] [--no-play] [--runs N]

import Foundation
import TTSKit

var modelsDir: String?
var tokenizerDir: String?
var outDir: String?
var text = "Welcome to the Cornell local AI hackathon. Everything you hear was generated on this laptop."
var play = true
var runs = 3
var it = CommandLine.arguments.dropFirst().makeIterator()
while let a = it.next() {
    switch a {
    case "--models": modelsDir = it.next()
    case "--tokenizer": tokenizerDir = it.next()
    case "--out": outDir = it.next()
    case "--text": text = it.next() ?? text
    case "--runs": runs = Int(it.next() ?? "") ?? runs
    case "--no-play": play = false
    default:
        print("usage: tts-smoke --models <dir> --tokenizer <dir> [--text ...] [--out dir] [--no-play] [--runs N]"); exit(2)
    }
}
guard let modelsDir, let tokenizerDir else { print("--models and --tokenizer are required"); exit(2) }

let tLoad = Date()
let config = TTSKitConfig(
    model: .qwen3TTS_0_6b,
    downloadBase: URL(fileURLWithPath: modelsDir),
    tokenizerFolder: URL(fileURLWithPath: tokenizerDir),
    verbose: false
)
let tts = try await TTSKit(config)
_ = try await tts.generate(text: "Warm up.", speaker: .ryan)   // forces model load + CoreML specialization
print(String(format: "TTSKit 0.6B loaded + warm-up in %.1fs", Date().timeIntervalSince(tLoad)))

final class FirstAudioClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: TimeInterval?
    let start = Date()
    func mark() { lock.lock(); if value == nil { value = Date().timeIntervalSince(start) }; lock.unlock() }
    var elapsed: TimeInterval? { lock.lock(); defer { lock.unlock() }; return value }
}

for i in 1...max(runs, 1) {
    let clock = FirstAudioClock()
    let t0 = clock.start
    let result = try await tts.generate(text: text, speaker: .ryan) { progress in
        if !progress.audio.isEmpty { clock.mark() }
        return true
    }
    let firstAudio = clock.elapsed
    let total = Date().timeIntervalSince(t0)
    let audioSecs = Double(result.audio.count) / Double(tts.sampleRate)
    print(String(format: "run %d: first audio %.0f ms | total %.2fs for %.2fs of audio (RTF %.2f)",
                 i, (firstAudio ?? 0) * 1000, total, audioSecs, total / max(audioSecs, 0.001)))
    if i == 1, let outDir {
        let url = try await AudioOutput.saveAudio(result.audio, toFolder: URL(fileURLWithPath: outDir),
                                                  filename: "tts_smoke", sampleRate: tts.sampleRate, format: .wav)
        print("saved \(url.path)")
    }
}

if play {
    print("Playing (streamed: playback starts while generating)...")
    let t0 = Date()
    _ = try await tts.play(text: text, speaker: .ryan)
    print(String(format: "play() returned after %.2fs", Date().timeIntervalSince(t0)))
}
