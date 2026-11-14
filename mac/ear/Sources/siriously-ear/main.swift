// siriously-ear: Siriously's speech daemon (PRD §4.1). Fully offline once models are on disk.
//
//   mic (16 kHz) → energy VAD (adaptive floor) → utterance
//        ├─ WhisperKit small.en ............ text
//        └─ SpeakerKit centroid embedding .. voice-lock score vs ~/.siriously/voiceprint.json
//   → POST http://127.0.0.1:8765/utterance {id, text, t_speech_end, stt_ms, speaker{verified, score, name}}
//
// Commands:
//   siriously-ear run        [--mic NAME] [--brain URL] [--no-lock] [--threshold X] [--silence-ms 800] [--save-audio]
//   siriously-ear enroll     [--n 10] [--mic NAME]      record N short phrases from you, then calibrate
//   siriously-ear calibrate  [--others a.wav ...]       rebuild the voiceprint from saved clips
//   siriously-ear score      a.wav b.wav ...            voice-lock score + transcript per file
//   siriously-ear devices                               list input devices
//
// Voice lock (validated in smoke/voice_session.py): embeddings of SHORT clips, centered on the mean of all
// short clips; score = cos(v-mu, me-mu) - cos(v-mu, other-mu); verified when score >= threshold.
// Threshold is calibrated leave-one-out over your clips vs impostor clips.

import AVFoundation
import Foundation
import SpeakerKit
import WhisperKit

setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - paths and options

let home = FileManager.default.homeDirectoryForCurrentUser.path
let root = ProcessInfo.processInfo.environment["SIRIOUSLY_ROOT"] ?? "\(home)/hackathon"
let modelsDir = "\(root)/models/argmax"
let stateDir = "\(home)/.siriously"
let enrollDir = "\(stateDir)/enroll"
let sessionDir = "\(root)/smoke/clips/real/session"
let sampleRate = 16000

struct Options {
    var cmd = "run"
    var mic = "MacBook Air Microphone"
    var brain = "http://127.0.0.1:8765/utterance"
    var stt = "small.en"
    var voiceprint = "\(stateDir)/voiceprint.json"
    var threshold: Float?
    var noLock = false
    var silenceMs = 800          // 600 split "look up … places in New York" at a natural pause
    var saveAudio = false
    var n = 10
    var others: [String] = []
    var files: [String] = []
}

func usage() -> Never {
    print("""
    usage: siriously-ear run|enroll|calibrate|score|devices [options]
      run        [--mic NAME] [--brain URL] [--no-lock] [--threshold X] [--silence-ms 800] [--save-audio] [--stt small.en]
      enroll     [--n 10] [--mic NAME]
      calibrate  [--others a.wav ...]
      score      file.wav ...
      devices
    """)
    exit(2)
}

var opt = Options()
var argv = Array(CommandLine.arguments.dropFirst())
if let first = argv.first, !first.hasPrefix("-") { opt.cmd = first; argv.removeFirst() }
var ai = argv.makeIterator()
while let a = ai.next() {
    switch a {
    case "--mic": opt.mic = ai.next() ?? opt.mic
    case "--brain": opt.brain = ai.next() ?? opt.brain
    case "--stt": opt.stt = ai.next() ?? opt.stt
    case "--voiceprint": opt.voiceprint = ai.next() ?? opt.voiceprint
    case "--threshold": opt.threshold = ai.next().flatMap { Float($0) }
    case "--no-lock": opt.noLock = true
    case "--silence-ms": opt.silenceMs = ai.next().flatMap { Int($0) } ?? opt.silenceMs
    case "--save-audio": opt.saveAudio = true
    case "--n": opt.n = ai.next().flatMap { Int($0) } ?? opt.n
    case "--others":
        while let f = ai.next() { if f.hasPrefix("--") { break }; opt.others.append(f) }
    case "-h", "--help": usage()
    default:
        if a.hasPrefix("-") { usage() }
        opt.files.append(a)
    }
}

func log(_ s: String) {
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
    print("[\(f.string(from: Date()))] \(s)")
}

func ms(since t: Date) -> Int { Int(Date().timeIntervalSince(t) * 1000) }

// MARK: - vector math

func cosine(_ a: [Float], _ b: [Float]) -> Float {
    var dot: Float = 0, na: Float = 0, nb: Float = 0
    for i in 0..<min(a.count, b.count) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i] }
    return (na > 0 && nb > 0) ? dot / (na.squareRoot() * nb.squareRoot()) : 0
}

func mean(_ vs: [[Float]]) -> [Float] {
    guard let d = vs.first?.count else { return [] }
    var m = [Float](repeating: 0, count: d)
    for v in vs { for i in 0..<d { m[i] += v[i] } }
    return m.map { $0 / Float(vs.count) }
}

func sub(_ a: [Float], _ b: [Float]) -> [Float] { zip(a, b).map { $0 - $1 } }

// MARK: - voiceprint

struct Voiceprint: Codable {
    var name: String
    var me: [Float]
    var other: [Float]
    var mu: [Float]
    var threshold: Float
    var margin: Float?
    var nMe: Int
    var nOther: Int
    var created: String

    func score(_ v: [Float]) -> Float {
        let c = sub(v, mu)
        return cosine(c, sub(me, mu)) - cosine(c, sub(other, mu))
    }
}

func loadVoiceprint(_ path: String) -> Voiceprint? {
    guard let d = FileManager.default.contents(atPath: path) else { return nil }
    return try? JSONDecoder().decode(Voiceprint.self, from: d)
}

/// Leave-one-out calibration over labelled short-clip embeddings.
func calibrate(me: [(String, [Float])], other: [(String, [Float])]) -> Voiceprint? {
    guard me.count >= 3, other.count >= 2 else {
        print("need >= 3 of your clips and >= 2 impostor clips (have \(me.count) / \(other.count))")
        return nil
    }
    let all = me.map { ($0.0, $0.1, true) } + other.map { ($0.0, $0.1, false) }
    var meScores: [Float] = [], otherScores: [Float] = []
    print("\nleave-one-out scores (score = sim(you) - sim(impostors), centered):")
    for (i, (label, v, isMe)) in all.enumerated() {
        let rest = all.enumerated().filter { $0.offset != i }.map { $0.element }
        let vp = Voiceprint(name: "", me: mean(rest.filter { $0.2 }.map { $0.1 }),
                            other: mean(rest.filter { !$0.2 }.map { $0.1 }), mu: mean(rest.map { $0.1 }),
                            threshold: 0, margin: nil, nMe: 0, nOther: 0, created: "")
        let s = vp.score(v)
        if isMe { meScores.append(s) } else { otherScores.append(s) }
        print(String(format: "  %@ %-28@ %+.3f", isMe ? "you  " : "other", (label as NSString).lastPathComponent, s))
    }
    let minMe = meScores.min()!, maxOther = otherScores.max()!
    let margin = minMe - maxOther
    // Separable: midpoint. Not separable: favour rejecting impostors (a missed command is cheaper than a hijack).
    let thr = margin > 0 ? (minMe + maxOther) / 2 : maxOther + 0.02
    let falseRejects = meScores.filter { $0 < thr }.count
    print(String(format: "\nyou min %+.3f | impostors max %+.3f | margin %+.3f | threshold %+.3f", minMe, maxOther, margin, thr))
    print(margin > 0.1 ? "voice lock: GOOD separation" :
          margin > 0 ? "voice lock: separable but tight; enroll more phrases in the demo room" :
          "voice lock: NOT separable on this data; \(falseRejects)/\(meScores.count) of your clips would be rejected")
    let iso = ISO8601DateFormatter().string(from: Date())
    return Voiceprint(name: "henrik", me: mean(me.map { $0.1 }), other: mean(other.map { $0.1 }),
                      mu: mean(all.map { $0.1 }), threshold: thr, margin: margin, nMe: me.count, nOther: other.count,
                      created: iso)
}

// MARK: - models

final class Ear {
    let whisper: WhisperKit
    let speaker: SpeakerKit
    let decode: DecodingOptions

    init(stt: String) async throws {
        let t = Date()
        let sttDir = "\(modelsDir)/models/argmaxinc/whisperkit-coreml/openai_whisper-\(stt)"
        guard FileManager.default.fileExists(atPath: sttDir) else { throw NSError(domain: "ear", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "STT model not found: \(sttDir)"]) }
        whisper = try await WhisperKit(WhisperKitConfig(
            modelFolder: sttDir, tokenizerFolder: URL(fileURLWithPath: "\(modelsDir)/tokenizers"),
            verbose: false, logLevel: .error, prewarm: true, load: true, download: false))
        speaker = try await SpeakerKit(PyannoteConfig(
            modelFolder: "\(modelsDir)/models/argmaxinc/speakerkit-coreml", download: false, load: true,
            verbose: false, logLevel: .error))
        decode = DecodingOptions(verbose: false, task: .transcribe, language: "en", temperature: 0,
                                 temperatureFallbackCount: 2, usePrefillPrompt: true, skipSpecialTokens: true,
                                 withoutTimestamps: true, suppressBlank: true, noSpeechThreshold: 0.6)
        log("models ready in \(ms(since: t)) ms (stt \(stt), SpeakerKit)")
        // Warm-up so the first real utterance isn't slow.
        let noise = (0..<sampleRate).map { _ in Float.random(in: -0.001...0.001) }
        _ = try? await whisper.transcribe(audioArray: noise, decodeOptions: decode)
        _ = try? await speaker.diarize(audioArray: noise, options: PyannoteDiarizationOptions(numberOfSpeakers: nil))
    }

    func transcribe(_ audio: [Float]) async throws -> String {
        let rs = try await whisper.transcribe(audioArray: audio, decodeOptions: decode)
        return cleanTranscript(rs.map(\.text).joined(separator: " "))
    }

    /// Centroid embedding of the dominant speaker (nil when no speech was found).
    func embed(_ audio: [Float]) async throws -> [Float]? {
        let r = try await speaker.diarize(audioArray: audio, options: PyannoteDiarizationOptions(numberOfSpeakers: nil))
        var talk: [Int: Float] = [:]
        for s in r.segments { if let id = s.speaker.speakerId { talk[id, default: 0] += s.endTime - s.startTime } }
        guard let dom = talk.max(by: { $0.value < $1.value })?.key else { return nil }
        return r.speakerCentroidEmbeddings[dom]
    }
}

let hallucinations: Set<String> = ["", "you", "thank you", "thanks for watching", "thank you for watching", "bye",
                                   "thank you so much", "subtitles by the amaraorg community", "so", "the end"]

func cleanTranscript(_ s: String) -> String {
    var t = s.replacingOccurrences(of: #"\[[^\]]*\]|\([^)]*\)|<\|[^|]*\|>"#, with: " ", options: .regularExpression)
    t = t.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    let norm = t.lowercased().filter { $0.isLetter || $0.isNumber || $0 == " " }
    return hallucinations.contains(norm.trimmingCharacters(in: .whitespaces)) ? "" : t
}

// MARK: - audio capture + VAD

/// Energy VAD over 50 ms frames. The noise floor is the 15th percentile of the last 5 s of frame energies,
/// so it follows the room (quiet flat vs. hackathon hall). Speech starts 10 dB above the floor, ends after
/// `silenceMs` below floor+6 dB. 300 ms pre-roll; utterances capped at 12 s.
final class Segmenter {
    let frame = 800
    let silenceFrames: Int
    var onUtterance: (([Float], Date) -> Void)?
    var onStart: (() -> Void)?
    private var pending: [Float] = []
    private var preroll: [[Float]] = []
    private var history: [Float] = []
    private var inSpeech = false
    private var speech: [Float] = []
    private var voiced = 0, silentRun = 0, loudRun = 0
    private(set) var floorDb: Float = -60
    private(set) var lastDb: Float = -90

    init(silenceMs: Int) { silenceFrames = max(4, silenceMs / 50) }

    func push(_ samples: [Float]) {
        pending.append(contentsOf: samples)
        var off = 0
        while pending.count - off >= frame {
            process(Array(pending[off..<(off + frame)]))
            off += frame
        }
        pending.removeFirst(off)
    }

    private func process(_ f: [Float]) {
        var e: Float = 0
        for x in f { e += x * x }
        let db = 10 * log10(e / Float(f.count) + 1e-10)
        lastDb = db
        history.append(db)
        if history.count > 100 { history.removeFirst(history.count - 100) }
        if history.count >= 10 { floorDb = history.sorted()[history.count * 15 / 100] }
        if !inSpeech {
            preroll.append(f)
            if preroll.count > 6 { preroll.removeFirst() }
            loudRun = (db > floorDb + 10 && db > -55) ? loudRun + 1 : 0
            if loudRun >= 3 {
                inSpeech = true
                speech = preroll.flatMap { $0 }
                voiced = loudRun; silentRun = 0; preroll = []
                onStart?()
            }
        } else {
            speech.append(contentsOf: f)
            if db > floorDb + 6 { voiced += 1; silentRun = 0 } else { silentRun += 1 }
            if silentRun >= silenceFrames || speech.count >= 12 * sampleRate {
                let end = Date().addingTimeInterval(-Double(silentRun) * 0.05)
                let keepTail = max(0, silentRun - 4) * frame             // keep 200 ms of trailing silence
                let clip = Array(speech.dropLast(keepTail))
                inSpeech = false; loudRun = 0; speech = []
                if voiced >= 5 { onUtterance?(clip, end) }                 // >= 250 ms of voiced audio
            }
        }
    }
}

/// Captures one specific microphone (by name) with AVCaptureSession, converted to 16 kHz mono Float32.
/// Unlike AVAudioEngine's input node, this never touches the system default input, so Bluetooth
/// headphones set as default (e.g. WH-1000XM4) neither silence the capture nor get forced into
/// their low-quality headset mode.
final class Mic: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    let seg: Segmenter
    private let session = AVCaptureSession()
    private let output = AVCaptureAudioDataOutput()
    private let q = DispatchQueue(label: "ear.capture")
    private(set) var deviceName = "?"
    private(set) var samplesSeen = 0
    private var settingsTimer: DispatchSourceTimer?

    init(seg: Segmenter) { self.seg = seg }

    static func devices() -> [AVCaptureDevice] {
        AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio,
                                         position: .unspecified).devices
    }

    func start(name: String) throws {
        let all = Mic.devices()
        guard let dev = all.first(where: { $0.localizedName.localizedCaseInsensitiveContains(name) })
                ?? AVCaptureDevice.default(for: .audio) else {
            throw NSError(domain: "ear", code: 2, userInfo: [NSLocalizedDescriptionKey: "no microphone found"])
        }
        if !dev.localizedName.localizedCaseInsensitiveContains(name) {
            log("mic \"\(name)\" not found, using \(dev.localizedName). Devices: \(all.map(\.localizedName))")
        }
        deviceName = dev.localizedName
        let input = try AVCaptureDeviceInput(device: dev)
        session.beginConfiguration()
        guard session.canAddInput(input) else { throw NSError(domain: "ear", code: 3,
            userInfo: [NSLocalizedDescriptionKey: "cannot open \(dev.localizedName)"]) }
        session.addInput(input)
        output.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: sampleRate,
                                AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 32,
                                AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false,
                                AVLinearPCMIsBigEndianKey: false]
        output.setSampleBufferDelegate(self, queue: q)
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        log("listening on \(dev.localizedName)")
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from connection: AVCaptureConnection) {
        var block: CMBlockBuffer?
        var abl = AudioBufferList()
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: &abl, bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil, flags: 0, blockBufferOut: &block)
        guard st == noErr, let data = abl.mBuffers.mData else { return }
        let n = Int(abl.mBuffers.mDataByteSize) / MemoryLayout<Float>.size
        let samples = Array(UnsafeBufferPointer(start: data.bindMemory(to: Float.self, capacity: n), count: n))
        samplesSeen += n
        seg.push(samples)
    }

    /// One-line health check: proves audio is flowing and shows the room level.
    func reportLevel(after seconds: Double) {
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { [self] in
            let secs = Double(samplesSeen) / Double(sampleRate)
            if samplesSeen == 0 {
                log("⚠️  no audio from \(deviceName) after \(Int(seconds)) s. Check System Settings → Privacy → Microphone")
            } else {
                log(String(format: "mic ok: %.1f s of audio, room level %.0f dBFS, noise floor %.0f dBFS",
                           secs, seg.lastDb, seg.floorDb))
            }
        }
    }

    func stop() { session.stopRunning() }

    /// Follows {"listening": true|false} in ~/.siriously/settings.json (the menu bar switch). Paused = the
    /// capture session is stopped, so macOS' orange mic indicator goes away too.
    func followListeningSetting() {
        let path = "\(stateDir)/settings.json"
        var paused = false
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "ear.settings"))
        settingsTimer = timer
        timer.schedule(deadline: .now() + 1, repeating: 1.0)
        timer.setEventHandler { [self] in
            guard let d = FileManager.default.contents(atPath: path),
                  let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
            let listening = (obj["listening"] as? Bool) ?? true
            if !listening && !paused {
                paused = true; session.stopRunning(); log("⏸  listening paused (menu bar / settings)")
            } else if listening && paused {
                paused = false; session.startRunning(); log("▶  listening resumed")
            }
        }
        timer.resume()
    }
}

func writeWav(_ samples: [Float], to path: String) throws {
    var d = Data()
    func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
    func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
    let n = UInt32(samples.count * 2)
    d.append("RIFF".data(using: .ascii)!); u32(36 + n); d.append("WAVEfmt ".data(using: .ascii)!)
    u32(16); u16(1); u16(1); u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
    d.append("data".data(using: .ascii)!); u32(n)
    for s in samples { u16(UInt16(bitPattern: Int16(max(-1, min(1, s)) * 32767))) }
    try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                            withIntermediateDirectories: true)
    try d.write(to: URL(fileURLWithPath: path))
}

func glob(_ dir: String, prefix: String) -> [String] {
    let fs = (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []
    return fs.filter { $0.hasPrefix(prefix) && $0.hasSuffix(".wav") }.sorted().map { "\(dir)/\($0)" }
}

// MARK: - overlay events (UDP, fire-and-forget; PRD §4.6 event shape)

final class OverlayEvents {
    private let fd = socket(AF_INET, SOCK_DGRAM, 0)
    private var addr = sockaddr_in()

    init(port: UInt16 = 8766) {
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    }

    func send(_ type: String, id: String? = nil, data: [String: Any] = [:], msSinceEnd: Int? = nil) {
        var ev: [String: Any] = ["type": type, "data": data]
        if let id { ev["utterance_id"] = id }
        if let msSinceEnd { ev["ms_since_speech_end"] = msSinceEnd }
        guard let d = try? JSONSerialization.data(withJSONObject: ev) else { return }
        d.withUnsafeBytes { buf in
            withUnsafePointer(to: &addr) { p in
                p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    _ = sendto(fd, buf.baseAddress, d.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }
}

// MARK: - brain client

struct Speaker: Codable { let verified: Bool; let score: Float?; let name: String; let threshold: Float? }
struct Utterance: Codable {
    let id: String
    let text: String
    let tSpeechEnd: Double
    let sttMs: Int
    let speaker: Speaker
    let audioS: Double
    let lockMs: Int
}

func post(_ u: Utterance, to url: String) async -> String {
    guard let u8 = URL(string: url) else { return "bad url" }
    var req = URLRequest(url: u8)
    req.httpMethod = "POST"
    req.timeoutInterval = 3
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    let enc = JSONEncoder(); enc.keyEncodingStrategy = .convertToSnakeCase
    req.httpBody = try? enc.encode(u)
    do {
        let (_, resp) = try await URLSession.shared.data(for: req)
        return "POST \((resp as? HTTPURLResponse)?.statusCode ?? 0)"
    } catch {
        return "brain not reachable"
    }
}

// MARK: - commands

func embedFiles(_ ear: Ear, _ paths: [String]) async -> [(String, [Float])] {
    var out: [(String, [Float])] = []
    for p in paths {
        guard let a = try? AudioProcessor.loadAudioAsFloatArray(fromPath: p) else { print("  !! cannot read \(p)"); continue }
        if let v = try? await ear.embed(a) { out.append((p, v)) } else { print("  !! no speech found in \(p)") }
    }
    return out
}

func runCalibrate(_ ear: Ear) async {
    let mePaths = glob(enrollDir, prefix: "me_") + glob(sessionDir, prefix: "henrik_cmd_")
    let otherPaths = opt.others.isEmpty ? glob(sessionDir, prefix: "other_cmd_") + glob(enrollDir, prefix: "other_")
                                        : opt.others
    print("your clips: \(mePaths.count), impostor clips: \(otherPaths.count)")
    let me = await embedFiles(ear, mePaths), other = await embedFiles(ear, otherPaths)
    guard let vp = calibrate(me: me, other: other) else { return }
    do {
        try FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
        let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted]
        try enc.encode(vp).write(to: URL(fileURLWithPath: opt.voiceprint))
        print("wrote \(opt.voiceprint)")
    } catch { print("could not write voiceprint: \(error)") }
}

let enrollPhrases = [
    "Open Slack", "Scroll down a bit", "Switch to Safari", "Start a new note called groceries",
    "Close this window", "Search for the hackathon schedule", "Undo that", "Open System Settings",
    "Send the message", "Create a new folder on the desktop", "Go to the next tab", "Play some music",
    "Reply saying I'm on my way", "Zoom in", "Open my downloads folder",
]

func runEnroll(_ ear: Ear) async throws {
    let seg = Segmenter(silenceMs: 700)
    let mic = Mic(seg: seg)
    let box = UtteranceBox()
    seg.onUtterance = { clip, _ in box.put(clip) }
    try mic.start(name: opt.mic)
    try await Task.sleep(nanoseconds: 1_200_000_000)                    // let the noise floor settle
    let start = glob(enrollDir, prefix: "me_").count
    print("\nEnrollment: say each phrase once, naturally, at your normal distance. \(opt.n) phrases.\n")
    var i = 0
    while i < opt.n {
        let phrase = enrollPhrases[(start + i) % enrollPhrases.count]
        print("  [\(i + 1)/\(opt.n)] say: \"\(phrase)\"  ", terminator: "")
        _ = box.take()
        guard let clip = await box.wait(timeout: 10) else { print("(nothing heard, again)"); continue }
        let path = String(format: "%@/me_%03d.wav", enrollDir, start + i + 1)
        try writeWav(clip, to: path)
        let text = (try? await ear.transcribe(clip)) ?? ""
        print(String(format: "✓ %.1fs  heard: \"%@\"", Double(clip.count) / Double(sampleRate), text))
        i += 1
    }
    mic.stop()
    await runCalibrate(ear)
}

final class UtteranceBox {
    private var clip: [Float]?
    private let lock = NSLock()
    func put(_ c: [Float]) { lock.lock(); clip = c; lock.unlock() }
    func take() -> [Float]? { lock.lock(); defer { clip = nil; lock.unlock() }; return clip }
    func wait(timeout: Double) async -> [Float]? {
        let t = Date()
        while Date().timeIntervalSince(t) < timeout {
            if let c = take() { return c }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return nil
    }
}

func runScore(_ ear: Ear) async {
    let vp = loadVoiceprint(opt.voiceprint)
    if vp == nil { print("no voiceprint at \(opt.voiceprint); showing transcripts only") }
    for p in opt.files {
        guard let a = try? AudioProcessor.loadAudioAsFloatArray(fromPath: p) else { print("cannot read \(p)"); continue }
        let t = Date()
        async let text = ear.transcribe(a)
        async let emb = ear.embed(a)
        let (tx, v) = (try? await text, try? await emb)
        let s = (v ?? nil).flatMap { vec in vp?.score(vec) }
        let verdict = s.map { $0 >= (opt.threshold ?? vp!.threshold) ? "ACCEPT" : "reject" } ?? "-"
        print(String(format: "%-28@ %@ %@  %4d ms  \"%@\"", (p as NSString).lastPathComponent,
                     s.map { String(format: "%+.3f", $0) } ?? "   -  ", verdict, ms(since: t), (tx ?? "") ?? ""))
    }
}

func runDaemon(_ ear: Ear) async throws {
    var vp: Voiceprint?
    if !opt.noLock {
        guard let v = loadVoiceprint(opt.voiceprint) else {
            print("No voiceprint at \(opt.voiceprint). Run `siriously-ear enroll` first, or pass --no-lock.")
            exit(1)
        }
        vp = v
        log(String(format: "voice lock ON: %@ (threshold %+.3f, %d/%d clips)", v.name, opt.threshold ?? v.threshold, v.nMe, v.nOther))
    } else {
        log("voice lock OFF (--no-lock): every utterance is marked verified")
    }
    let seg = Segmenter(silenceMs: opt.silenceMs)
    let mic = Mic(seg: seg)
    let (stream, cont) = AsyncStream<([Float], Date)>.makeStream()
    let overlay = OverlayEvents()
    seg.onStart = { log("… speech"); overlay.send("listening") }
    seg.onUtterance = { clip, end in cont.yield((clip, end)) }
    try mic.start(name: opt.mic)
    mic.reportLevel(after: 3)
    mic.followListeningSetting()
    log("brain: \(opt.brain)   end-of-speech silence: \(opt.silenceMs) ms   Ctrl-C to stop")
    var n = 0
    let runTag: String = { let f = DateFormatter(); f.dateFormat = "HHmmss"; return f.string(from: Date()) }()
    for await (clip, end) in stream {
        n += 1
        let id = String(format: "u-%@-%04d", runTag, n)          // unique across restarts (saved audio)
        let t0 = Date()
        async let textTask: String = ear.transcribe(clip)
        async let embTask: [Float]? = vp == nil ? nil : ear.embed(clip)
        let tLock = Date()
        let emb = (try? await embTask) ?? nil
        let lockMs = vp == nil ? 0 : ms(since: tLock)
        let text = (try? await textTask) ?? ""
        let sttMs = ms(since: t0)
        let secs = Double(clip.count) / Double(sampleRate)
        if opt.saveAudio { try? writeWav(clip, to: "\(stateDir)/utterances/\(id).wav") }
        if text.isEmpty {
            overlay.send("dropped", id: id)
            log(String(format: "[%@] %.1fs  (no words, dropped)  %d ms", id, secs, sttMs)); continue
        }
        let score = emb.flatMap { vp?.score($0) }
        let thr = opt.threshold ?? vp?.threshold ?? 0
        let verified = vp == nil ? true : (score.map { $0 >= thr } ?? false)
        let u = Utterance(id: id, text: text, tSpeechEnd: end.timeIntervalSince1970, sttMs: sttMs,
                          speaker: Speaker(verified: verified, score: score, name: verified ? (vp?.name ?? "anyone") : "unknown",
                                           threshold: vp == nil ? nil : thr),
                          audioS: secs, lockMs: lockMs)
        let status = await post(u, to: opt.brain)
        log(String(format: "[%@] %.1fs  \"%@\"  stt %d ms  %@  e2e %d ms  %@", id, secs, text, sttMs,
                   score.map { String(format: "voice %+.2f %@", $0, verified ? "✓" : "✗ REJECTED") } ?? (vp == nil ? "lock off" : "voice ? ✗"),
                   Int(Date().timeIntervalSince(end) * 1000), status))
    }
}

// MARK: - main

do {
    switch opt.cmd {
    case "devices":
        for d in Mic.devices() { print(d.localizedName) }
    case "run":
        try await runDaemon(try await Ear(stt: opt.stt))
    case "enroll":
        try await runEnroll(try await Ear(stt: opt.stt))
    case "calibrate":
        await runCalibrate(try await Ear(stt: opt.stt))
    case "score":
        if opt.files.isEmpty { usage() }
        await runScore(try await Ear(stt: opt.stt))
    default:
        usage()
    }
} catch {
    print("error: \(error.localizedDescription)")
    exit(1)
}
