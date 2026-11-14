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
import Network

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

    private func targetFrame() -> NSRect {
        effect.layoutSubtreeIfNeeded()
        let h = max(36, ceil(effect.fittingSize.height))
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
    private var current: String?
    private var busyUntil = Date.distantPast
    private var listening = false

    private var busy: Bool { Date() < busyUntil }

    func handle(_ ev: [String: Any]) {
        guard let type = str(ev["type"]) else { return }
        let uid = str(ev["utterance_id"])
        let data = ev["data"] as? [String: Any] ?? [:]
        let ms = num(ev["ms_since_speech_end"])
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
                hud.setDetail("No matching action", symbol: "minus", hideAfter: 1.6)
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

// MARK: - main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
let udp = UDPInput(port: cfg.udpPort) { controller.handle($0) }
let ws = WSInput(url: cfg.ws) { controller.handle($0) }
print("siriously-overlay running (rejected: \(cfg.rejected), listening indicator: \(cfg.showListening ? "on" : "off"))")
if cfg.demo { runDemo(controller) }
app.run()
