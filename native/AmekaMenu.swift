// Ameka Voice — the menu bar face of the daemon.
// Draws the Ameka mark, shows what is listening and to which session, and lets
// you mute, switch session, and read the log without touching a terminal.
import AppKit
import Foundation

let DAEMON = "http://127.0.0.1:8765"
let AQUA = NSColor(red: 0.498, green: 1.0, blue: 0.878, alpha: 1.0)

// MARK: - the mark

func amekaMark(size: CGFloat, template: Bool) -> NSImage {
    let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
        let s = rect.width / 64.0
        func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint {
            NSPoint(x: x * s, y: (64 - y) * s)          // SVG y-down -> AppKit y-up
        }
        let path = NSBezierPath()
        path.move(to: p(24, 8))
        for pt in [(40.0, 8.0), (40, 24), (56, 24), (56, 40), (40, 40),
                   (40, 56), (24, 56), (24, 40), (8, 40), (8, 24), (24, 24)] {
            path.line(to: p(CGFloat(pt.0), CGFloat(pt.1)))
        }
        path.close()
        path.lineWidth = 5 * s
        path.lineJoinStyle = .round
        NSColor.black.setStroke()
        path.stroke()

        let squares = NSBezierPath()
        squares.appendRect(NSRect(x: 46 * s, y: (64 - 14) * s, width: 6 * s, height: 6 * s))
        squares.appendRect(NSRect(x: 46 * s, y: (64 - 56) * s, width: 6 * s, height: 6 * s))
        (template ? NSColor.black : AQUA).setFill()
        squares.fill()
        return true
    }
    image.isTemplate = template
    return image
}

// MARK: - daemon

struct Active {
    var key = ""
    var host = ""
    var title = ""
    var spoken = ""
    var state = ""
    var idle = 0
    var loopOn = true
    var minutes = 10.0
    var nextIn = 0
}

struct State {
    var running = false
    var active: [Active] = []
    var muted = false
    var chat = false
    var bound = ""
    var sessions: [String] = []
    var waiting: [String] = []
    var takeaway = ""
    var project = ""
    var voice = ""
    var hearing = ""
}

func fetchState(_ done: @escaping (State) -> Void) {
    guard let url = URL(string: "\(DAEMON)/state") else { return done(State()) }
    var req = URLRequest(url: url)
    req.timeoutInterval = 2.5
    URLSession.shared.dataTask(with: req) { data, _, _ in
        var st = State()
        if let data = data,
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            st.running = true
            st.muted = json["muted"] as? Bool ?? false
            st.chat = json["chat"] as? Bool ?? false
            st.bound = json["bound"] as? String ?? ""
            st.sessions = json["sessions"] as? [String] ?? []
            if let waiting = json["waiting"] as? [[String: Any]] {
                st.waiting = waiting.compactMap { $0["project"] as? String }
            }
            if let last = json["last"] as? [String: Any] {
                st.takeaway = last["takeaway"] as? String ?? ""
                st.project = last["project"] as? String ?? ""
            }
            if let engines = json["engines"] as? [String: Any] {
                st.voice = engines["voice"] as? String ?? ""
                st.hearing = engines["hearing"] as? String ?? ""
            }
            if let rows = json["active"] as? [[String: Any]] {
                st.active = rows.map { r in
                    var a = Active()
                    a.key = r["key"] as? String ?? ""
                    a.host = r["host"] as? String ?? ""
                    a.title = r["title"] as? String ?? ""
                    a.spoken = r["spoken"] as? String ?? ""
                    a.state = r["state"] as? String ?? ""
                    a.idle = r["idle_minutes"] as? Int ?? 0
                    if let loop = r["loop"] as? [String: Any] {
                        a.loopOn = loop["on"] as? Bool ?? true
                        a.minutes = loop["minutes"] as? Double ?? 10
                    }
                    a.nextIn = r["next_in"] as? Int ?? 0
                    return a
                }
            }
        }
        DispatchQueue.main.async { done(st) }
    }.resume()
}

func post(_ path: String, _ body: [String: Any], _ done: (() -> Void)? = nil) {
    guard let url = URL(string: DAEMON + path) else { return }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.timeoutInterval = 3
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try? JSONSerialization.data(withJSONObject: body)
    URLSession.shared.dataTask(with: req) { _, _, _ in
        DispatchQueue.main.async { done?() }
    }.resume()
}

@discardableResult
func shell(_ args: [String]) -> Int32 {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/bash")
    task.arguments = ["-lc", args.joined(separator: " ")]
    try? task.run()
    task.waitUntilExit()
    return task.terminationStatus
}

// MARK: - menu bar

final class Controller: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var state = State()
    var timer: Timer?

    func start() {
        item.button?.image = amekaMark(size: 18, template: true)
        item.button?.imagePosition = .imageOnly
        item.button?.toolTip = "Ameka Voice"
        menu.delegate = self
        item.menu = menu
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in self.refresh() }
    }

    func refresh() {
        fetchState { st in
            self.state = st
            self.item.button?.alphaValue = st.running ? (st.muted ? 0.35 : 1.0) : 0.25
            self.item.button?.toolTip = st.running
                ? (st.muted ? "Ameka Voice — muted" : "Ameka Voice — listening")
                : "Ameka Voice — not running"
            if self.menu.numberOfItems > 0 { self.build() }
        }
    }

    func menuWillOpen(_ menu: NSMenu) { build() }

    private func info(_ text: String) -> NSMenuItem {
        let entry = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        entry.isEnabled = false
        return entry
    }

    func build() {
        menu.removeAllItems()

        let title = NSMenuItem(title: "Ameka Voice", action: nil, keyEquivalent: "")
        title.isEnabled = false
        title.attributedTitle = NSAttributedString(
            string: "Ameka Voice",
            attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold)])
        menu.addItem(title)

        if !state.running {
            menu.addItem(info("Not running"))
            menu.addItem(.separator())
            menu.addItem(NSMenuItem(title: "Start Ameka", action: #selector(startDaemon),
                                    keyEquivalent: "").bind(self))
            menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q").bind(self))
            return
        }

        menu.addItem(info(state.muted ? "Muted" : (state.chat ? "Listening" : "Listening after briefings")))
        if !state.bound.isEmpty { menu.addItem(info("Talking to \(state.bound)")) }
        if !state.voice.isEmpty {
            menu.addItem(info("Voice \(state.voice) · hearing \(state.hearing) · on this Mac"))
        }

        if !state.takeaway.isEmpty {
            menu.addItem(.separator())
            let head = state.project.isEmpty ? "Last" : "Last · \(state.project)"
            menu.addItem(info(head))
            menu.addItem(info("  " + truncate(state.takeaway, 58)))
        }

        if !state.active.isEmpty {
            menu.addItem(.separator())
            let looping = state.active.filter { $0.loopOn }.count
            let sessions = NSMenuItem(title: "Sessions · \(state.active.count) active, \(looping) on smart loop",
                                      action: nil, keyEquivalent: "")
            let submenu = NSMenu()
            // Grouped by where they live: the Claude app, the ChatGPT app, a browser.
            let hosts = Array(NSOrderedSet(array: state.active.map { $0.host })) as? [String] ?? []
            for host in hosts {
                submenu.addItem(info(host))
                for a in state.active where a.host == host {
                    let what = a.state.isEmpty ? "" : " · \(a.state)"
                    let idle = a.idle > 0 ? " · \(a.idle)m idle" : ""
                    let loop = a.loopOn ? String(format: "next smart loop in %d:%02d", a.nextIn / 60, a.nextIn % 60) : "smart loop off"
                    let row = NSMenuItem(title: "  \(truncate(a.title, 40))  —  \(a.spoken)\(what)\(idle)  ·  \(loop)",
                                         action: nil, keyEquivalent: "")
                    let controls = NSMenu()
                    let toggle = NSMenuItem(title: a.loopOn ? "Smart loop on — click to turn off" : "Smart loop off — click to turn on",
                                            action: #selector(toggleLoop(_:)), keyEquivalent: "")
                    toggle.target = self
                    toggle.representedObject = a.key
                    toggle.state = a.loopOn ? .on : .off
                    controls.addItem(toggle)
                    controls.addItem(.separator())
                    controls.addItem(info("Check every"))
                    for m in [2, 5, 10, 20, 30, 60] {
                        let every = NSMenuItem(title: "  \(m) minutes", action: #selector(setMinutes(_:)), keyEquivalent: "")
                        every.target = self
                        every.representedObject = ["key": a.key, "minutes": m]
                        every.state = Int(a.minutes) == m ? .on : .off
                        controls.addItem(every)
                    }
                    controls.addItem(.separator())
                    let talk = NSMenuItem(title: "Talk to this session", action: #selector(bind(_:)), keyEquivalent: "")
                    talk.target = self
                    talk.representedObject = a.spoken
                    controls.addItem(talk)
                    row.submenu = controls
                    submenu.addItem(row)
                }
            }
            submenu.addItem(.separator())
            let allOn = NSMenuItem(title: "All smart loops on", action: #selector(allLoops(_:)), keyEquivalent: "")
            allOn.target = self; allOn.representedObject = true; submenu.addItem(allOn)
            let allOff = NSMenuItem(title: "All smart loops off", action: #selector(allLoops(_:)), keyEquivalent: "")
            allOff.target = self; allOff.representedObject = false; submenu.addItem(allOff)
            sessions.submenu = submenu
            menu.addItem(sessions)
        }
        if !state.waiting.isEmpty {
            menu.addItem(info("Waiting: " + state.waiting.joined(separator: ", ")))
        }

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: state.muted ? "Unmute" : "Mute",
                                action: #selector(toggleMute), keyEquivalent: "m").bind(self))
        menu.addItem(NSMenuItem(title: "Open Log", action: #selector(openLog), keyEquivalent: "l").bind(self))
        menu.addItem(NSMenuItem(title: "Your Brain…", action: #selector(openBrain), keyEquivalent: "b").bind(self))
        menu.addItem(NSMenuItem(title: "Open Settings File", action: #selector(openConfig), keyEquivalent: ",").bind(self))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Restart Ameka", action: #selector(restart), keyEquivalent: "r").bind(self))
        menu.addItem(NSMenuItem(title: "Quit Ameka", action: #selector(quit), keyEquivalent: "q").bind(self))
    }

    func truncate(_ text: String, _ limit: Int) -> String {
        text.count <= limit ? text : String(text.prefix(limit - 1)) + "…"
    }

    @objc func toggleMute() { post("/mute", ["muted": !state.muted]) { self.refresh() } }
    @objc func toggleLoop(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        let on = sender.state != .on
        post("/loop", ["key": key, "on": on]) { self.refresh() }
    }
    @objc func setMinutes(_ sender: NSMenuItem) {
        guard let d = sender.representedObject as? [String: Any],
              let key = d["key"] as? String, let m = d["minutes"] as? Int else { return }
        post("/loop", ["key": key, "minutes": m, "on": true]) { self.refresh() }
    }
    @objc func allLoops(_ sender: NSMenuItem) {
        let on = sender.representedObject as? Bool ?? true
        post("/loop", ["all": true, "on": on]) { self.refresh() }
    }
    @objc func bind(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        post("/bind", ["project": name]) { self.refresh() }
    }
    @objc func openBrain() {
        // Who the user is and what they are working toward — read by every smart loop.
        DispatchQueue.global().async { shell(["~/.local/bin/ameka", "you", "edit"]) }
    }
    @objc func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath:
            NSHomeDirectory() + "/.local/state/ameka/ameka.log"))
    }
    @objc func openConfig() {
        NSWorkspace.shared.open(URL(fileURLWithPath: NSHomeDirectory() + "/.config/ameka/config.toml"))
    }
    @objc func startDaemon() {
        shell(["launchctl", "kickstart", "gui/\(getuid())/ai.ameka.voice"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
    }
    @objc func restart() {
        shell(["launchctl", "kickstart", "-k", "gui/\(getuid())/ai.ameka.voice"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.refresh() }
    }
    @objc func quit() { NSApp.terminate(nil) }
}

extension NSMenuItem {
    func bind(_ target: AnyObject) -> NSMenuItem { self.target = target; return self }
}

// One icon in the menu bar, however many ways the app gets launched.
let running = NSWorkspace.shared.runningApplications.filter {
    $0.bundleIdentifier == Bundle.main.bundleIdentifier
}
if running.count > 1 {
    let mine = ProcessInfo.processInfo.processIdentifier
    let older = running.contains { $0.processIdentifier < mine && !$0.isTerminated }
    if older {
        exit(0)                       // an instance is already showing the mark
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
controller.start()
app.run()
