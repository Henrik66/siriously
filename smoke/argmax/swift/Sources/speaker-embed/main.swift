// Speaker-embedding go/no-go test for "live captions that name each speaker".
//
// Every clip is diarized in its own independent `diarize()` call. We then compare
// SpeakerKit's per-speaker centroid embeddings (DiarizationResult.speakerCentroidEmbeddings,
// raw embedder space, public since argmax-oss-swift v1.1.0) across runs with cosine similarity.
//
// Usage:
//   speaker-embed --models <dir> [--out result.json] \
//       --enroll henrik=A1.wav --enroll henrik=A2.wav --enroll ryan=B1.wav --enroll ryan=B2.wav \
//       --test mix=AB.wav@2
//
// --enroll name=path   single-voice clip; its dominant speaker's centroid is that person's voiceprint
// --test label=path@N  multi-voice clip (N = known speaker count, optional); each speaker is
//                      matched to the nearest enrolled voiceprint

import Foundation
import SpeakerKit
import WhisperKit

struct ClipReport: Codable {
    let role: String
    let label: String
    let path: String
    let audioSeconds: Double
    let diarizeSeconds: Double
    let speakerCount: Int
    let speakingSeconds: [String: Double]
    let segments: [[Double]]
    let centroidDims: [String: Int]
    let centroids: [String: [Float]]
}

struct Match: Codable {
    let clip: String
    let speakerId: Int
    let speakingSeconds: Double
    let best: String
    let bestSimilarity: Float
    let secondBest: String?
    let secondSimilarity: Float?
}

struct Report: Codable {
    let clips: [ClipReport]
    let sameVoice: [[String]]
    let differentVoice: [[String]]
    let minSameSimilarity: Float?
    let maxDifferentSimilarity: Float?
    let suggestedThreshold: Float?
    let matches: [Match]
    let verdict: String
}

func cosine(_ a: [Float], _ b: [Float]) -> Float {
    var dot: Float = 0, na: Float = 0, nb: Float = 0
    for i in 0..<min(a.count, b.count) {
        dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]
    }
    return (na > 0 && nb > 0) ? dot / (na.squareRoot() * nb.squareRoot()) : 0
}

func usage() -> Never {
    print("usage: speaker-embed --models <dir> [--out file.json] --enroll name=path.wav ... [--test label=path.wav[@N] ...]")
    exit(2)
}

// ---- argument parsing
var modelsDir: String?
var outPath: String?
var enrollSpecs: [(String, String)] = []
var testSpecs: [(String, String, Int?)] = []
var it = CommandLine.arguments.dropFirst().makeIterator()
while let arg = it.next() {
    switch arg {
    case "--models": modelsDir = it.next()
    case "--out": outPath = it.next()
    case "--enroll", "--test":
        guard let spec = it.next(), let eq = spec.firstIndex(of: "=") else { usage() }
        let name = String(spec[..<eq])
        var path = String(spec[spec.index(after: eq)...])
        var n: Int?
        if let at = path.lastIndex(of: "@"), let v = Int(path[path.index(after: at)...]) {
            n = v
            path = String(path[..<at])
        }
        if arg == "--enroll" { enrollSpecs.append((name, path)) } else { testSpecs.append((name, path, n)) }
    default: usage()
    }
}
guard let modelsDir, !enrollSpecs.isEmpty else { usage() }

// ---- load models (downloads to --models on first run; offline afterwards)
let tLoad = Date()
let kit = try await SpeakerKit(PyannoteConfig(downloadBase: modelsDir, load: true, verbose: false))
print(String(format: "SpeakerKit ready in %.1fs (models under %@)", Date().timeIntervalSince(tLoad), modelsDir))

func run(role: String, label: String, path: String, speakers: Int?) async throws -> ClipReport {
    let audio = try AudioProcessor.loadAudioAsFloatArray(fromPath: path)
    let t = Date()
    let r = try await kit.diarize(audioArray: audio, options: PyannoteDiarizationOptions(numberOfSpeakers: speakers))
    let dt = Date().timeIntervalSince(t)
    var talk: [String: Double] = [:]
    var segs: [[Double]] = []
    for s in r.segments {
        guard let id = s.speaker.speakerId else { continue }
        talk["\(id)", default: 0] += Double(s.endTime - s.startTime)
        segs.append([Double(id), Double(s.startTime), Double(s.endTime)])
    }
    var dims: [String: Int] = [:]
    var cents: [String: [Float]] = [:]
    for (id, c) in r.speakerCentroidEmbeddings { dims["\(id)"] = c.count; cents["\(id)"] = c }
    let secs = Double(audio.count) / 16000.0
    print(String(format: "[%@] %@: %.1fs audio, diarized in %.2fs (RTF %.3f), %d speaker(s), talk time %@, centroid dims %@",
                 role, label, secs, dt, dt / max(secs, 0.001), r.speakerCount,
                 talk.sorted { $0.key < $1.key }.map { "\($0.key)=\(String(format: "%.1f", $0.value))s" }.joined(separator: " "),
                 dims.sorted { $0.key < $1.key }.map { "\($0.key):\($0.value)" }.joined(separator: " ")))
    return ClipReport(role: role, label: label, path: path, audioSeconds: secs, diarizeSeconds: dt,
                      speakerCount: r.speakerCount, speakingSeconds: talk, segments: segs,
                      centroidDims: dims, centroids: cents)
}

// ---- enrollment: one voiceprint per clip (dominant speaker)
var clips: [ClipReport] = []
var prints: [(name: String, clip: String, vec: [Float])] = []
for (i, (name, path)) in enrollSpecs.enumerated() {
    let label = "\(name)#\(i + 1)"
    let c = try await run(role: "enroll", label: label, path: path, speakers: nil)
    clips.append(c)
    guard let dominant = c.speakingSeconds.max(by: { $0.value < $1.value })?.key, let vec = c.centroids[dominant] else {
        print("  !! no centroid for \(label); skipping"); continue
    }
    if c.speakerCount > 1 { print("  note: \(label) has \(c.speakerCount) speakers; using dominant speaker \(dominant)") }
    prints.append((name, label, vec))
}

// ---- cross-run similarity between enrollment clips
print("\nCross-run cosine similarity between enrollment voiceprints:")
var same: [[String]] = [], diff: [[String]] = []
var sameVals: [Float] = [], diffVals: [Float] = []
for i in 0..<prints.count {
    for j in (i + 1)..<prints.count {
        let s = cosine(prints[i].vec, prints[j].vec)
        let isSame = prints[i].name == prints[j].name
        print(String(format: "  %@ %@ vs %@: %.3f", isSame ? "SAME" : "diff", prints[i].clip, prints[j].clip, s))
        let row = [prints[i].clip, prints[j].clip, String(format: "%.4f", s)]
        if isSame { same.append(row); sameVals.append(s) } else { diff.append(row); diffVals.append(s) }
    }
}
let minSame = sameVals.min(), maxDiff = diffVals.max()
var threshold: Float?
if let a = minSame, let b = maxDiff { threshold = (a + b) / 2 }

// ---- test clips: match each diarized speaker to the nearest enrolled voice (mean voiceprint per name)
var byName: [String: [Float]] = [:]
for name in Set(prints.map(\.name)) {
    let vs = prints.filter { $0.name == name }.map(\.vec)
    byName[name] = (0..<vs[0].count).map { k in vs.map { $0[k] }.reduce(0, +) / Float(vs.count) }
}
var matches: [Match] = []
for (label, path, n) in testSpecs {
    let c = try await run(role: "test", label: label, path: path, speakers: n)
    clips.append(c)
    for (id, vec) in c.centroids.sorted(by: { $0.key < $1.key }) {
        let ranked = byName.map { ($0.key, cosine(vec, $0.value)) }.sorted { $0.1 > $1.1 }
        guard let best = ranked.first else { continue }
        let second = ranked.count > 1 ? ranked[1] : nil
        let m = Match(clip: label, speakerId: Int(id) ?? -1, speakingSeconds: c.speakingSeconds[id] ?? 0,
                      best: best.0, bestSimilarity: best.1, secondBest: second?.0, secondSimilarity: second?.1)
        matches.append(m)
        let accepted = threshold.map { best.1 >= $0 } ?? true
        print(String(format: "  %@ speaker %@ (%.1fs) -> %@ (%.3f)%@%@", label, id, m.speakingSeconds, best.0, best.1,
                     second.map { String(format: ", runner-up %@ (%.3f)", $0.0, $0.1) } ?? "",
                     accepted ? "" : "  [below threshold -> unknown]"))
    }
}

// ---- verdict
var verdict = "INCONCLUSIVE: need >=2 clips of the same voice and >=1 different voice"
if let a = minSame, let b = maxDiff {
    let margin = a - b
    verdict = margin >= 0.15 ? "WORKS" : (margin > 0 ? "PARTIAL" : "NO")
    verdict += String(format: ": min same-voice %.3f vs max different-voice %.3f (margin %.3f, threshold ~%.3f)", a, b, margin, threshold ?? 0)
}
print("\nVERDICT: \(verdict)")

let report = Report(clips: clips, sameVoice: same, differentVoice: diff, minSameSimilarity: minSame,
                    maxDifferentSimilarity: maxDiff, suggestedThreshold: threshold, matches: matches, verdict: verdict)
if let outPath {
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    try enc.encode(report).write(to: URL(fileURLWithPath: outPath))
    print("wrote \(outPath)")
}
