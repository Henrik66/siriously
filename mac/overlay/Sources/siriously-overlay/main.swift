// siriously-overlay: a quiet status HUD for Siriously (PRD §4.6).
//
// A small translucent panel in the top-right corner, under the menu bar. Hidden when idle. It never takes
// focus or clicks (the executor's keystrokes must keep going to the user's app).
//
// Inputs (same event shape for both: {"type", "utterance_id", "data": {...}, "ms_since_speech_end"}):
//   - UDP 127.0.0.1:8766  from the speech daemon (heard / rejected / listening / dropped) and the mini brain
//   - WS  ws://127.0.0.1:8765/events  from the real brain (route / plan / action / done / error / confirm)
//
//   siriously-overlay [--rejected off|commands|all] [--show-listening] [--ws URL] [--udp-port 8766] [--demo]

import AppKit
import Combine
import Network
import SwiftUI

setvbuf(stdout, nil, _IOLBF, 0)

struct Config {
    var udpPort: UInt16 = 8766
    var ws = "ws://127.0.0.1:8765/events"
    var rejected = "commands"          // off | commands (only command-like phrases) | all
    var showListening = false
    var demo = false
}

var cfg = Config()
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    switch a {
    case "--udp-port": cfg.udpPort = args.next().flatMap { UInt16($0) } ?? cfg.udpPort
    case "--ws": cfg.ws = args.next() ?? cfg.ws
    case "--rejected": cfg.rejected = args.next() ?? cfg.rejected
    case "--show-listening": cfg.showListening = true
    case "--demo": cfg.demo = true
    default:
        print("usage: siriously-overlay [--rejected off|commands|all] [--show-listening] [--ws URL] [--udp-port N] [--demo]")
        exit(2)
    }
}

// MARK: - Panel

final class RoundedEffectView: NSVisualEffectView {
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateBorder()
    }

    func updateBorder() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            self.layer?.borderColor = NSColor.labelColor.withAlphaComponent(0.09).cgColor
        }
    }
}

enum SymbolMotion { case none, variable, pulse }

final class HUD {
    let width: CGFloat = 320
    private let panel: NSPanel
    private let effect = RoundedEffectView()
    private let icon = NSImageView()
    private let title = NSTextField(labelWithString: "")
    private let detail = NSTextField(labelWithString: "")
    private var hideItem: DispatchWorkItem?
    private(set) var isShown = false
    private(set) var titleText = ""

    init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: 40),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.alphaValue = 0

        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 12
        effect.layer?.cornerCurve = .continuous
        effect.layer?.masksToBounds = true
        effect.layer?.borderWidth = 0.5
        effect.updateBorder()
        panel.contentView = effect

        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        icon.contentTintColor = .secondaryLabelColor
        icon.setContentHuggingPriority(.required, for: .horizontal)

        for (f, size, color) in [(title, 13.0, NSColor.labelColor), (detail, 11.5, NSColor.secondaryLabelColor)] {
            f.font = .systemFont(ofSize: size)
            f.textColor = color
            f.lineBreakMode = .byTruncatingTail
            f.maximumNumberOfLines = 1
            f.cell?.truncatesLastVisibleLine = true
            f.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        detail.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)
        // The title may wrap to two lines so questions/confirmations always show the full name.
        title.lineBreakMode = .byWordWrapping
        title.maximumNumberOfLines = 2
        title.preferredMaxLayoutWidth = width - 13 - 14 - 16 - 9

        let text = NSStackView(views: [title, detail])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 1
        let row = NSStackView(views: [icon, text])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 9
        row.edgeInsets = NSEdgeInsets(top: 9, left: 13, bottom: 9, right: 14)
        row.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(row)
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            row.topAnchor.constraint(equalTo: effect.topAnchor),
            row.bottomAnchor.constraint(equalTo: effect.bottomAnchor),
            icon.widthAnchor.constraint(equalToConstant: 16),
            row.widthAnchor.constraint(equalToConstant: width),
        ])
    }

    /// Show or update. `detail: nil` hides the second line. `hideAfter: nil` stays until the next update.
    func show(symbol: String, motion: SymbolMotion = .none, title t: String, secondaryTitle: Bool = false,
              detail d: String? = nil, tint: NSColor = .secondaryLabelColor, hideAfter: TimeInterval? = nil) {
        hideItem?.cancel()
        let changedSymbol = icon.image?.accessibilityDescription != symbol
        if changedSymbol {
            icon.removeAllSymbolEffects()
            icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: symbol)
        }
        icon.contentTintColor = tint
        if changedSymbol || motion == .none {
            switch motion {
            case .variable: icon.addSymbolEffect(.variableColor.iterative.dimInactiveLayers, options: .repeating)
            case .pulse: icon.addSymbolEffect(.pulse, options: .repeating)
            case .none: icon.removeAllSymbolEffects()
            }
        }
        titleText = t
        title.stringValue = t
        title.textColor = secondaryTitle ? .secondaryLabelColor : .labelColor
        detail.stringValue = d ?? ""
        detail.isHidden = (d ?? "").isEmpty
        layout(animated: isShown)
        if !isShown { fadeIn() }
        if let h = hideAfter {
            let item = DispatchWorkItem { [weak self] in self?.hide() }
            hideItem = item
            DispatchQueue.main.asyncAfter(deadline: .now() + h, execute: item)
        }
    }

    /// Update the second line (and symbol) of the visible HUD. Auto-hides after 15 s if nothing follows.
    func setDetail(_ d: String?, symbol: String? = nil, motion: SymbolMotion = .none,
                   tint: NSColor = .secondaryLabelColor, hideAfter: TimeInterval = 15) {
        guard isShown else { return }
        show(symbol: symbol ?? (icon.image?.accessibilityDescription ?? "waveform"), motion: motion,
             title: titleText, secondaryTitle: title.textColor == .secondaryLabelColor, detail: d,
             tint: tint, hideAfter: hideAfter)
    }

    func hide() {
        hideItem?.cancel()
        guard isShown else { return }
        isShown = false
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.35
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self, !self.isShown else { return }
            self.panel.orderOut(nil)
            self.icon.removeAllSymbolEffects()
        })
    }

    /// Height from the text itself (Auto Layout under-reports wrapped NSTextField height).
    private func contentHeight() -> CGFloat {
        let w = title.preferredMaxLayoutWidth
        let oneLine = title.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: 10_000, height: 100)).height
        let th = min(title.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: w, height: 400)).height, oneLine * 2 + 1)
        let dh = detail.isHidden ? 0 : detail.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: w, height: 100)).height + 1
        return ceil(th + dh + 18)
    }

    private func targetFrame() -> NSRect {
        effect.layoutSubtreeIfNeeded()
        let h = max(36, contentHeight())
        let screen = NSScreen.screens.first ?? NSScreen.main!
        let vf = screen.visibleFrame
        return NSRect(x: vf.maxX - width - 10, y: vf.maxY - h - 8, width: width, height: h)
    }

    private func layout(animated: Bool) {
        let f = targetFrame()
        if animated, f.size != panel.frame.size {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.18
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(f, display: true)
            }
        } else {
            panel.setFrame(f, display: true)
        }
        panel.invalidateShadow()
    }

    private func fadeIn() {
        isShown = true
        var f = panel.frame
        f.origin.y += 4                                     // settle 4 px downwards while fading in
        panel.setFrame(f, display: false)
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.2
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().alphaValue = 1
            panel.animator().setFrame(targetFrame(), display: true)
        }
    }
}

// MARK: - Event → HUD

func str(_ v: Any?) -> String? {
    if let s = v as? String, !s.isEmpty { return s }
    return nil
}

func num(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }

func pretty(_ text: String) -> String {
    var t = text.trimmingCharacters(in: .whitespacesAndNewlines)
    while let last = t.last, ".!".contains(last) { t.removeLast() }
    return t.prefix(1).uppercased() + t.dropFirst()
}

let modifierOrder = ["ctrl": 0, "control": 0, "option": 1, "alt": 1, "opt": 1, "shift": 2, "cmd": 3, "command": 3]
let modifierGlyph = ["ctrl": "⌃", "control": "⌃", "option": "⌥", "alt": "⌥", "opt": "⌥", "shift": "⇧", "cmd": "⌘", "command": "⌘", "fn": "fn "]
let keyGlyph = ["return": "↩", "enter": "↩", "esc": "⎋", "escape": "⎋", "tab": "⇥", "space": "Space", "delete": "⌫",
                "backspace": "⌫", "forward-delete": "⌦", "up": "↑", "down": "↓", "left": "←", "right": "→",
                "pageup": "⇞", "pagedown": "⇟", "home": "↖", "end": "↘"]

func keycaps(_ spec: String) -> String {
    var parts = spec.lowercased().split(separator: "+", omittingEmptySubsequences: false).map(String.init)
    if spec.hasSuffix("++") { parts = parts.filter { !$0.isEmpty } + ["+"] }
    guard let key = parts.popLast() else { return spec }
    let mods = parts.sorted { (modifierOrder[$0] ?? 9) < (modifierOrder[$1] ?? 9) }.map { modifierGlyph[$0] ?? $0 }
    return mods.joined() + (keyGlyph[key] ?? key.uppercased())
}

func describe(_ a: [String: Any], label: String?) -> String {
    switch str(a["op"]) ?? "" {
    case "open_app": return "Opening \(pretty(str(a["app"]) ?? "app").capitalized)"
    case "press": return label.map { "Clicking “\($0)”" } ?? "Clicking"
    case "menu": return label ?? "Choosing a menu item"
    case "type": return "Typing “\(str(a["text"]) ?? "")”"
    case "keys": return "Pressing \(keycaps(str(a["keys"]) ?? ""))"
    case "scroll": return "Scrolling \(str(a["direction"]) ?? "down")"
    case "wait": return "Waiting"
    default: return str(a["op"]) ?? "Working"
    }
}

let commandVerbs: Set<String> = ["open", "close", "delete", "remove", "send", "start", "create", "make", "switch",
                                 "scroll", "undo", "empty", "move", "quit", "type", "search", "new", "go", "play",
                                 "reply", "click", "press", "select", "launch", "show", "rename", "save", "copy",
                                 "paste", "turn", "set", "shut", "restart", "lock", "log", "email", "message",
                                 "call", "find", "erase", "trash", "format", "install", "uninstall", "buy", "pay"]

func looksLikeCommand(_ text: String) -> Bool {
    let words = text.lowercased().split { !$0.isLetter && $0 != "'" }
    guard let first = words.first, words.count <= 12 else { return false }
    let skip: Set<String> = ["please", "hey", "ok", "okay", "siri", "siriously", "can", "you", "could"]
    let firstReal = words.first { !skip.contains(String($0)) } ?? first
    return commandVerbs.contains(String(firstReal))
}

final class Controller {
    let hud = HUD()
    var menu: MenuBar?
    private var current: String?
    private var busyUntil = Date.distantPast
    private var listening = false

    private var busy: Bool { Date() < busyUntil }

    func handle(_ ev: [String: Any]) {
        guard let type = str(ev["type"]) else { return }
        let uid = str(ev["utterance_id"])
        let data = ev["data"] as? [String: Any] ?? [:]
        let ms = num(ev["ms_since_speech_end"])
        menu?.store.record(type: type, uid: uid, data: data)
        let mine = uid == nil || uid == current

        switch type {
        case "listening":
            guard cfg.showListening, !busy else { return }
            listening = true
            hud.show(symbol: "waveform", motion: .variable, title: "Listening", secondaryTitle: true, hideAfter: 12)
        case "dropped":
            if listening && !busy { listening = false; hud.hide() }
        case "heard":
            guard let text = str(data["text"]) else { return }
            if uid == current && hud.isShown { return }       // daemon and brain both report "heard"
            current = uid
            listening = false
            busyUntil = Date().addingTimeInterval(4)
            hud.show(symbol: "waveform", title: pretty(text), detail: nil, tint: .secondaryLabelColor, hideAfter: 4)
        case "rejected":
            listening = false
            guard !busy, let text = str(data["text"]) else { if !busy { hud.hide() }; return }
            let show = cfg.rejected == "all" || (cfg.rejected == "commands" && looksLikeCommand(text))
            guard show else { if hud.isShown { hud.hide() }; return }
            hud.show(symbol: "lock", title: "Not your voice", secondaryTitle: true, detail: "“\(pretty(text))”",
                     hideAfter: 2.4)
        case "route":
            guard mine else { return }
            switch str(data["route"]) ?? "" {
            case "planner":
                busyUntil = Date().addingTimeInterval(15)
                hud.setDetail("Thinking", symbol: "ellipsis", motion: .variable)
            case "none":
                busyUntil = .distantPast
                hud.setDetail(str(data["say"]) ?? "No matching action", symbol: "minus", hideAfter: 2.2)
            default: break
            }
        case "plan":
            guard mine else { return }
            busyUntil = Date().addingTimeInterval(15)
            if let say = str(data["say"]) { hud.setDetail(say, symbol: "waveform") }
        case "action":
            guard mine else { return }
            busyUntil = Date().addingTimeInterval(15)
            let a = data["action"] as? [String: Any] ?? data
            let text = str(data["desc"]) ?? describe(a, label: str(data["label"]))
            let i = Int(num(data["index"]) ?? 0), n = Int(num(data["of"]) ?? 0)
            hud.setDetail(n > 1 ? "\(text)  ·  \(i)/\(n)" : text, symbol: "arrow.forward")
        case "done":
            guard mine else { return }
            busyUntil = .distantPast
            let say = str(data["say"]) ?? "Done"
            let t = ms.map { String(format: "%.1f s", $0 / 1000) }
            hud.setDetail(t.map { "\(say)  ·  \($0)" } ?? say, symbol: "checkmark", tint: .labelColor, hideAfter: 1.8)
        case "error":
            guard mine else { return }
            busyUntil = .distantPast
            let msg = str(data["message"]) ?? str(data["error"]) ?? "Something went wrong"
            if hud.isShown {
                hud.show(symbol: "exclamationmark.triangle", title: hud.titleText, detail: msg,
                         tint: .systemOrange, hideAfter: 3.5)
            } else {
                hud.show(symbol: "exclamationmark.triangle", title: msg, tint: .systemOrange, hideAfter: 3.5)
            }
        case "ask":
            busyUntil = Date().addingTimeInterval(20)
            let options = (data["options"] as? [String]) ?? []
            let detail = options.isEmpty ? "Say your answer"
                : options.prefix(4).enumerated().map { "\($0.offset + 1) \($0.element)" }.joined(separator: "   ")
            hud.show(symbol: "text.bubble", title: str(data["say"]) ?? "Which one?", detail: detail,
                     tint: .labelColor, hideAfter: 20)
        case "confirm":
            busyUntil = Date().addingTimeInterval(12)
            hud.show(symbol: "questionmark.circle", title: str(data["say"]) ?? "Are you sure?",
                     detail: "Say yes or no", tint: .labelColor, hideAfter: 12)
        default:
            break
        }
    }
}

// MARK: - Inputs

final class UDPInput {
    private var listener: NWListener?
    init(port: UInt16, onEvent: @escaping ([String: Any]) -> Void) {
        let params = NWParameters.udp
        params.requiredInterfaceType = .loopback
        params.allowLocalEndpointReuse = true
        do {
            let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
            l.newConnectionHandler = { conn in
                conn.start(queue: .main)
                func receive() {
                    conn.receiveMessage { data, _, _, err in
                        if let d = data, let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                            onEvent(obj)
                        }
                        if err == nil { receive() }
                    }
                }
                receive()
            }
            l.stateUpdateHandler = { st in
                if case .failed(let e) = st { print("udp :\(port) failed: \(e)") }
                if case .ready = st { print("listening for events on udp 127.0.0.1:\(port)") }
            }
            l.start(queue: .main)
            listener = l
        } catch {
            print("udp :\(port) unavailable: \(error)")
        }
    }
}

final class WSInput {
    private let url: URL
    private let onEvent: ([String: Any]) -> Void
    private var connected = false
    private var announcedDown = false

    init(url: String, onEvent: @escaping ([String: Any]) -> Void) {
        self.url = URL(string: url)!
        self.onEvent = onEvent
        connect()
    }

    private func connect() {
        let task = URLSession.shared.webSocketTask(with: url)
        task.resume()
        receive(task)
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                if !self.connected { self.connected = true; self.announcedDown = false; print("brain events: connected \(self.url)") }
                var data: Data?
                switch msg {
                case .string(let s): data = s.data(using: .utf8)
                case .data(let d): data = d
                @unknown default: break
                }
                if let d = data, let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                    DispatchQueue.main.async { self.onEvent(obj) }
                }
                self.receive(task)
            case .failure:
                if self.connected || !self.announcedDown {
                    print("brain events: not connected (\(self.url)), retrying every 3 s")
                    self.announcedDown = true
                }
                self.connected = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.connect() }
            }
        }
    }
}

// MARK: - Demo

func runDemo(_ c: Controller) {
    let id = "u-demo"
    let script: [(Double, [String: Any])] = [
        (0.5, ["type": "heard", "utterance_id": id, "data": ["text": "start a new note called groceries."]]),
        (0.6, ["type": "plan", "utterance_id": id, "data": ["say": "New note “groceries”", "steps": 3]]),
        (0.5, ["type": "action", "utterance_id": id, "data": ["index": 1, "of": 3, "action": ["op": "open_app", "app": "notes"]]]),
        (0.8, ["type": "action", "utterance_id": id, "data": ["index": 2, "of": 3, "action": ["op": "keys", "keys": "cmd+n"]]]),
        (0.7, ["type": "action", "utterance_id": id, "data": ["index": 3, "of": 3, "action": ["op": "type", "text": "groceries"]]]),
        (0.6, ["type": "done", "utterance_id": id, "ms_since_speech_end": 2400]),
        (3.0, ["type": "rejected", "utterance_id": "u-demo2", "data": ["text": "Delete everything on the desktop."]]),
        (3.5, ["type": "heard", "utterance_id": "u-demo3", "data": ["text": "Find the flight confirmation and forward it to Sam"]]),
        (0.6, ["type": "route", "utterance_id": "u-demo3", "data": ["route": "planner"]]),
        (2.5, ["type": "confirm", "utterance_id": "u-demo3", "data": ["say": "Forward “Flight confirmation” to Sam?"]]),
        (4.0, ["type": "error", "utterance_id": "u-demo3", "data": ["message": "Mail is not in front; nothing was sent"]]),
    ]
    var t = 0.0
    for (dt, ev) in script {
        t += dt
        DispatchQueue.main.asyncAfter(deadline: .now() + t) { c.handle(ev) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + t + 5) { runDemo(c) }
}

// MARK: - Menu bar (status item + popover)

let siriouslyDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".siriously")
let settingsURL = siriouslyDir.appendingPathComponent("settings.json")

struct PermissionCategory: Identifiable { let id: String; let label: String }
let permissionCategories = [
    PermissionCategory(id: "send_messages", label: "Sending messages"),
    PermissionCategory(id: "delete", label: "Deleting"),
    PermissionCategory(id: "purchases", label: "Purchases"),
    PermissionCategory(id: "system", label: "Shut down, restart, log out"),
    PermissionCategory(id: "quit_apps", label: "Quitting apps"),
    PermissionCategory(id: "trash", label: "Moving to Trash"),
]

enum RecentStatus { case running, waiting, done, error, rejected }

struct RecentItem: Identifiable {
    let id: String
    var text: String
    var status: RecentStatus
    var detail: String
    let time: Date
}

/// Reads and writes ~/.siriously/settings.json (shared with the brain and the speech daemon; reloaded on change).
final class SettingsStore: ObservableObject {
    @Published var listening = true { didSet { if !loading { save() } } }
    @Published var wakeWordRequired = true { didSet { if !loading { save() } } }
    @Published var permissions: [String: String] = [:] { didSet { if !loading { save() } } }
    @Published var recent: [RecentItem] = []
    @Published var working = false
    private(set) var wakeWord = "siriously"
    private var raw: [String: Any] = [:]
    private var mtime: Date?
    private var loading = false

    init() {
        load()
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in self?.reloadIfChanged() }
    }

    private func reloadIfChanged() {
        let m = (try? FileManager.default.attributesOfItem(atPath: settingsURL.path))?[.modificationDate] as? Date
        if m != mtime { load() }
    }

    func load() {
        mtime = (try? FileManager.default.attributesOfItem(atPath: settingsURL.path))?[.modificationDate] as? Date
        guard let d = try? Data(contentsOf: settingsURL),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        loading = true
        raw = obj
        listening = (obj["listening"] as? Bool) ?? true
        wakeWordRequired = (obj["wake_word_required"] as? Bool) ?? true
        wakeWord = (obj["wake_word"] as? String) ?? "siriously"
        permissions = (obj["permissions"] as? [String: String]) ?? [:]
        loading = false
    }

    func save() {
        raw["listening"] = listening
        raw["wake_word_required"] = wakeWordRequired
        var p = (raw["permissions"] as? [String: String]) ?? [:]
        for (k, v) in permissions { p[k] = v }
        raw["permissions"] = p
        try? FileManager.default.createDirectory(at: siriouslyDir, withIntermediateDirectories: true)
        if let d = try? JSONSerialization.data(withJSONObject: raw, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: settingsURL, options: .atomic)
            mtime = (try? FileManager.default.attributesOfItem(atPath: settingsURL.path))?[.modificationDate] as? Date
        }
    }

    func binding(for category: String) -> Binding<String> {
        Binding(get: { self.permissions[category] ?? "ask" }, set: { self.permissions[category] = $0 })
    }

    var statusLine: String {
        if !listening { return "Paused" }
        return wakeWordRequired ? "Listening for “\(wakeWord.capitalized)”" : "Listening to everything you say"
    }

    var voiceLockLine: String {
        let url = siriouslyDir.appendingPathComponent("voiceprint.json")
        guard let d = try? Data(contentsOf: url), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let n = o["nMe"] as? Int else { return "Voice lock not set up" }
        return "Voice lock on · \(n) samples of your voice"
    }

    /// Fold overlay events into the "Recent" list and the working indicator.
    func record(type: String, uid: String?, data: [String: Any]) {
        let id = uid ?? UUID().uuidString
        func update(_ f: (inout RecentItem) -> Void) {
            if let i = recent.firstIndex(where: { $0.id == id }) { f(&recent[i]) }
        }
        switch type {
        case "heard":
            guard let text = data["text"] as? String, !text.hasPrefix("Listening") else { return }
            if recent.contains(where: { $0.id == id }) { return }
            recent.insert(RecentItem(id: id, text: pretty(text), status: .running, detail: "", time: Date()), at: 0)
            working = true
        case "rejected":
            guard let text = data["text"] as? String else { return }
            recent.insert(RecentItem(id: id, text: pretty(text), status: .rejected, detail: "Not your voice", time: Date()), at: 0)
        case "plan":
            update { $0.detail = (data["say"] as? String) ?? $0.detail }
        case "confirm", "ask":
            update { $0.status = .waiting; $0.detail = (data["say"] as? String) ?? "" }
            working = false
        case "done":
            update { $0.status = .done; if let s = data["say"] as? String { $0.detail = s } }
            working = false
        case "route":
            if (data["route"] as? String) == "none" {
                update { $0.status = .error; $0.detail = (data["say"] as? String) ?? "No matching action" }
                working = false
            }
        case "error":
            update { $0.status = .error; $0.detail = (data["message"] as? String) ?? "Error" }
            working = false
        default:
            break
        }
        if recent.count > 6 { recent.removeLast(recent.count - 6) }
    }
}

struct RecentRow: View {
    let item: RecentItem
    var symbol: (String, Color) {
        switch item.status {
        case .running: return ("ellipsis", .secondary)
        case .waiting: return ("questionmark.circle", .secondary)
        case .done: return ("checkmark", .primary)
        case .error: return ("exclamationmark.triangle", .orange)
        case .rejected: return ("lock", .secondary)
        }
    }
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbol.0).foregroundStyle(symbol.1).frame(width: 14)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.text).lineLimit(1).truncationMode(.tail)
                    .foregroundStyle(item.status == .rejected ? .secondary : .primary)
                if !item.detail.isEmpty {
                    Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 4)
            Text(item.time, format: .dateTime.hour().minute()).font(.caption).monospacedDigit()
                .foregroundStyle(.tertiary)
        }
    }
}

struct MenuView: View {
    @ObservedObject var store: SettingsStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: store.listening ? "waveform" : "waveform.slash")
                    .font(.system(size: 15, weight: .medium)).frame(width: 22)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Siriously").font(.system(size: 13, weight: .semibold))
                    Text(store.statusLine).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("Listening", isOn: $store.listening).toggleStyle(.switch).labelsHidden().controlSize(.small)
            }
            .padding(.bottom, 12)

            Toggle(isOn: $store.wakeWordRequired) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("Only respond to “Siriously”")
                    Text("Follow-ups work for a few seconds without it").font(.caption).foregroundStyle(.secondary)
                }
            }
            .toggleStyle(.switch).controlSize(.small)
            .disabled(!store.listening)

            Divider().padding(.vertical, 12)

            Text("Permissions").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.bottom, 6)
            VStack(spacing: 7) {
                ForEach(permissionCategories) { c in
                    HStack {
                        Text(c.label).lineLimit(1)
                        Spacer(minLength: 8)
                        Picker(c.label, selection: store.binding(for: c.id)) {
                            Text("Allow").tag("allow")
                            Text("Ask").tag("ask")
                            Text("Block").tag("block")
                        }
                        .pickerStyle(.segmented).labelsHidden().controlSize(.small).frame(width: 138)
                    }
                }
            }

            Divider().padding(.vertical, 12)

            Text("Recent").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.bottom, 6)
            if store.recent.isEmpty {
                Text(store.wakeWordRequired ? "Say “Siriously, open Safari”" : "Say “open Safari”")
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(store.recent.prefix(5)) { RecentRow(item: $0) }
                }
            }

            Divider().padding(.vertical, 12)

            HStack {
                Text(store.voiceLockLine).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.borderless).font(.caption)
            }
        }
        .font(.system(size: 12.5))
        .padding(14)
        .frame(width: 330)
    }
}

final class MenuBar: NSObject {
    let store = SettingsStore()
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let popover = NSPopover()
    private var observers: [AnyCancellable] = []

    override init() {
        super.init()
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = NSHostingController(rootView: MenuView(store: store))
        if let b = item.button {
            b.target = self
            b.action = #selector(toggle)
            b.toolTip = "Siriously"
        }
        updateIcon()
        observers.append(store.$listening.sink { [weak self] _ in DispatchQueue.main.async { self?.updateIcon() } })
        observers.append(store.$working.sink { [weak self] _ in DispatchQueue.main.async { self?.updateIcon() } })
    }

    private func updateIcon() {
        let name = !store.listening ? "waveform.slash" : (store.working ? "waveform.circle.fill" : "waveform")
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "Siriously")
            ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: "Siriously")
        img?.isTemplate = true
        item.button?.image = img
    }

    @objc private func toggle() {
        if popover.isShown {
            popover.performClose(nil)
        } else if let b = item.button {
            store.load()
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
        }
    }
}

// MARK: - main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
let menuBar = MenuBar()
controller.menu = menuBar
let udp = UDPInput(port: cfg.udpPort) { controller.handle($0) }
let ws = WSInput(url: cfg.ws) { controller.handle($0) }
print("siriously-overlay running (rejected: \(cfg.rejected), listening indicator: \(cfg.showListening ? "on" : "off"))")
if cfg.demo { runDemo(controller) }
app.run()
