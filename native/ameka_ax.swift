// ameka-ax — read and drive apps through the accessibility API.
//
// Chromium keeps web content out of the accessibility tree until a client asks
// for it, and drops it again when the renderer reloads — an account switch, a
// sign-in, an update. Setting AXManualAccessibility on the application turns
// it on at runtime, so no menu toggle and no relaunch is needed.
//
//   ameka-ax read  <app>                 dump the web text of the front window
//   ameka-ax probe <app>                 report whether web content is reachable
//   ameka-ax click <app> <title> [--group <project>]
//                                        click the sidebar ROW with that title,
//                                        scrolling the sidebar and expanding the
//                                        project group to reach it. Never the
//                                        header. Prints "yes <text>" or "no".
//   ameka-ax find  <app> <title> [--group <project>]   like click, only report.
//   ameka-ax hold  <app>                 stay alive so the tree stays built.
//   ameka-ax text  <app>                 every label in the front window: "x<TAB>text".
//
// click exists because doing the same through System Events walked a thousand
// elements with three round trips each and took a minute per switch.
import AppKit
import ApplicationServices

let args = CommandLine.arguments
guard args.count >= 3 else {
    print("usage: ameka-ax <read|probe|click|find|text|field|hold|windows> <app name> [name ...]")
    exit(2)
}
let mode = args[1]
let appName = args[2]
let wanted = Array(args.dropFirst(3)).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }

guard let app = NSWorkspace.shared.runningApplications.first(where: {
    ($0.localizedName ?? "") == appName || ($0.bundleIdentifier ?? "") == appName
}) else {
    FileHandle.standardError.write("app-not-running\n".data(using: .utf8)!)
    exit(3)
}

let axApp = AXUIElementCreateApplication(app.processIdentifier)

func wake() {
    AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(axApp, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
}
wake()
usleep(400_000)

func copyAttr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func role(_ element: AXUIElement) -> String {
    (copyAttr(element, kAXRoleAttribute as String) as? String) ?? ""
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (copyAttr(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}

func label(_ element: AXUIElement) -> String {
    for key in [kAXValueAttribute as String, kAXTitleAttribute as String,
                kAXDescriptionAttribute as String] {
        if let value = copyAttr(element, key) as? String {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
    }
    return ""
}

func frame(_ element: AXUIElement) -> CGRect? {
    guard let p = copyAttr(element, kAXPositionAttribute as String),
          let s = copyAttr(element, kAXSizeAttribute as String) else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

func findWebArea(_ element: AXUIElement, depth: Int = 0) -> AXUIElement? {
    if depth > 40 { return nil }
    if role(element) == "AXWebArea" { return element }
    for child in children(element) {
        if let hit = findWebArea(child, depth: depth + 1) { return hit }
    }
    return nil
}

func text(of element: AXUIElement, into lines: inout [String], depth: Int = 0) {
    if depth > 60 || lines.count > 4000 { return }
    let r = role(element)
    if r == "AXStaticText" || r == "AXTextArea" || r == "AXTextField" || r == "AXHeading" {
        let l = label(element)
        if !l.isEmpty { lines.append(l) }
    }
    for child in children(element) { text(of: child, into: &lines, depth: depth + 1) }
}

func frontWindow() -> AXUIElement? {
    // The window can be gone for a moment while the app rebuilds it after the
    // tree is switched on, and the tree can be empty for a moment after that.
    for _ in 0..<8 {
        if let windows = copyAttr(axApp, kAXWindowsAttribute as String) as? [AXUIElement],
           let front = windows.first {
            return front
        }
        wake()
        usleep(500_000)
    }
    return nil
}

func count(_ element: AXUIElement, depth: Int = 0, budget: inout Int) {
    if depth > 60 || budget <= 0 { return }
    for child in children(element) { budget -= 1; count(child, depth: depth + 1, budget: &budget) }
}

func populated(_ window: AXUIElement) -> Bool {
    var budget = 40
    count(window, budget: &budget)
    return budget <= 0
}

// ------------------------------------------------------------------ click --
struct Hit { let element: AXUIElement; let text: String; let rect: CGRect }

func collect(_ element: AXUIElement, edge: CGFloat, into hits: inout [Hit], depth: Int = 0) {
    if depth > 60 || hits.count > 3000 { return }
    let l = label(element)
    if !l.isEmpty, l.count < 120, let r = frame(element), r.width > 0, r.height > 0, r.minX < edge {
        hits.append(Hit(element: element, text: l, rect: r))
    }
    for child in children(element) { collect(child, edge: edge, into: &hits, depth: depth + 1) }
}

func press(_ hit: Hit) -> Bool {
    // A real click, the way a person would. Electron answers an accessibility
    // press with "success" and does nothing — the sidebar row was "pressed"
    // and the pane never changed — where a mouse event at the row's centre
    // switches the session every time.
    let point = CGPoint(x: hit.rect.midX, y: hit.rect.midY)
    if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left),
       let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
       let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) {
        move.post(tap: .cghidEventTap); usleep(40_000)
        down.post(tap: .cghidEventTap); usleep(60_000); up.post(tap: .cghidEventTap)
        return true
    }
    // No event source: fall back to the accessibility press, up the tree.
    var current: AXUIElement? = hit.element
    for _ in 0..<6 {
        guard let el = current else { break }
        var actions: CFArray?
        if AXUIElementCopyActionNames(el, &actions) == .success,
           let names = actions as? [String], names.contains(kAXPressAction as String),
           AXUIElementPerformAction(el, kAXPressAction as CFString) == .success {
            return true
        }
        current = copyAttr(el, kAXParentAttribute as String).map { $0 as! AXUIElement }
    }
    return false
}

// text: every label in the front window with its x position, one per line, so
// the caller can tell what is in the sidebar (small x) from what is in the
// transcript (large x) — and check that the session on screen is the one meant.
if mode == "text" {
    app.activate(options: [])
    guard let window = frontWindow() else { print("no window"); exit(4) }
    var hits: [Hit] = []
    for _ in 0..<16 {
        if let wf = frame(window) { collect(window, edge: wf.maxX + 1, into: &hits) }
        if hits.count > 4 { break }
        wake(); usleep(600_000); hits.removeAll()
    }
    for h in hits { print("\(Int(h.rect.minX))\t\(h.text.replacingOccurrences(of: "\n", with: " "))") }
    exit(0)
}

func scrollSidebar(_ window: AXUIElement, notches: Int32) {
    // A wheel event over the sidebar, where a person would put the pointer.
    guard let wf = frame(window) else { return }
    let point = CGPoint(x: wf.minX + 120, y: wf.minY + wf.height * 0.55)
    if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left) {
        move.post(tap: .cghidEventTap)
    }
    if let ev = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: -notches, wheel2: 0, wheel3: 0) {
        ev.location = point
        ev.post(tap: .cghidEventTap)
    }
    usleep(350_000)
}

func rows(_ window: AXUIElement) -> [Hit] {
    var hits: [Hit] = []
    guard let wf = frame(window) else { return hits }
    collect(window, edge: wf.minX + 280, into: &hits)      // the sidebar, not the transcript
    return hits
}

func findRow(_ hits: [Hit], _ title: String) -> Hit? {
    // A session row is a static text in the sidebar. The project header is a
    // button, and clicking it is not opening the session — the first version
    // did exactly that, reported success, and typed into whatever was showing.
    let texts = hits.filter { role($0.element) == "AXStaticText" && $0.rect.height >= 10 }
    if let exact = texts.first(where: { $0.text == title }) { return exact }
    return texts.first(where: { $0.text.localizedCaseInsensitiveContains(title) })
}

// field: put the cursor in the page's message box. ChatGPT, Claude, Gemini all
// draw one text area near the bottom with a placeholder like "Ask anything" or
// "Message …"; focusing it is what lets a paste and a Return land in it.
// hold: stay alive as an assistive client so the app keeps its accessibility
// tree built. Chromium builds the tree when something asks and tears it down
// when the last client goes away — and every other mode here is a process that
// exits, so the next one arrives to "no tree" and a click that finds no rows.
// One of these, running, and the tree is simply always there.
// windows: how many real windows this app has on screen, asked of the window
// server rather than of accessibility. The accessibility tree reports none
// whenever it has been torn down, and a window that is plainly there was read
// as "the app has no window" — which stopped her even trying.
if mode == "windows" {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                          kCGNullWindowID) as? [[String: Any]] ?? []
    let mine = list.filter {
        ($0["kCGWindowOwnerName"] as? String ?? "") == appName
            && ($0["kCGWindowLayer"] as? Int ?? 99) == 0
            && (($0["kCGWindowBounds"] as? [String: Any])?["Height"] as? Double ?? 0) > 200
    }
    print(mine.count)
    exit(0)
}

if mode == "hold" {
    wake()                                  // once: setting it repeatedly made
    usleep(500_000)                         // the tree churn and read empty
    while true {
        // Reading is what counts as being a client. Only ask again for the
        // flag when the tree has actually gone, which is the case this is for.
        let windows = copyAttr(axApp, kAXWindowsAttribute as String) as? [AXUIElement]
        if let front = windows?.first {
            _ = children(front).count
        } else {
            wake()
        }
        sleep(3)
    }
}

if mode == "field" {
    app.activate(options: [])
    guard let window = frontWindow() else { print("no window"); exit(4) }
    var boxes: [(AXUIElement, CGRect, String)] = []
    func gather(_ el: AXUIElement, depth: Int) {
        if depth > 60 || boxes.count > 200 { return }
        let r = role(el)
        if r == "AXTextArea" || r == "AXTextField" || r == "AXComboBox" {
            if let f = frame(el), f.width > 120, f.height > 14 {
                let hint = ((copyAttr(el, "AXPlaceholderValue") as? String) ?? "") + " " + label(el)
                boxes.append((el, f, hint))
            }
        }
        for c in children(el) { gather(c, depth: depth + 1) }
    }
    for attempt in 0..<12 {
        gather(window, depth: 0)
        if !boxes.isEmpty { break }
        wake(); usleep(500_000)
        if attempt == 11 { print("no field"); exit(0) }
    }
    let wanted = ["ask", "message", "send", "reply", "type", "chat", "prompt", "talk"]
    let scored = boxes.sorted { a, b in
        let sa = wanted.contains { a.2.lowercased().contains($0) } ? 1 : 0
        let sb = wanted.contains { b.2.lowercased().contains($0) } ? 1 : 0
        if sa != sb { return sa > sb }
        return a.1.maxY > b.1.maxY                 // lowest on the page wins
    }
    guard let best = scored.first else { print("no field"); exit(0) }
    AXUIElementSetAttributeValue(best.0, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(150_000)
    let point = CGPoint(x: best.1.midX, y: best.1.midY)
    if let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
       let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) {
        down.post(tap: .cghidEventTap); usleep(50_000); up.post(tap: .cghidEventTap)
    }
    print("yes \(best.2.trimmingCharacters(in: .whitespaces).prefix(40))")
    exit(0)
}

if mode == "click" || mode == "find" {
    guard let title = wanted.first, !title.isEmpty else { print("no names"); exit(2) }
    var group = ""
    if let i = wanted.firstIndex(of: "--group"), i + 1 < wanted.count { group = wanted[i + 1] }
    app.activate(options: [])
    guard let window = frontWindow() else { print("no window"); exit(4) }
    var hits = rows(window)
    var waited = 0
    while hits.count <= 4 && waited < 16 {                 // the tree is being rebuilt
        wake(); usleep(600_000); hits = rows(window); waited += 1
    }
    if hits.isEmpty { print("no tree"); exit(0) }
    var found = findRow(hits, title)
    // Not rendered: scroll the sidebar down and look again, a screen at a time.
    var scrolled = 0
    while found == nil && scrolled < 10 {
        scrollSidebar(window, notches: 6); scrolled += 1
        hits = rows(window); found = findRow(hits, title)
    }
    // Still not: the project group may be collapsed. Open it, then look again.
    if found == nil && !group.isEmpty,
       let header = hits.first(where: { role($0.element) == "AXButton" && $0.text.lowercased() == group.lowercased() }) {
        _ = press(header); usleep(500_000)
        hits = rows(window); found = findRow(hits, title)
        var more = 0
        while found == nil && more < 6 {
            scrollSidebar(window, notches: 6); more += 1
            hits = rows(window); found = findRow(hits, title)
        }
    }
    if scrolled > 0 || found == nil {
        // put the sidebar back where it was, whatever happened
        for _ in 0..<(scrolled + 6) { scrollSidebar(window, notches: -6) }
        if found != nil {                                   // and find the row again where it now is
            hits = rows(window); found = findRow(hits, title)
            var again = 0
            while found == nil && again < 10 { scrollSidebar(window, notches: 6); again += 1; hits = rows(window); found = findRow(hits, title) }
        }
    }
    guard let hit = found else { print("no"); exit(0) }
    if mode == "find" {
        let r = hit.rect
        print("yes \(hit.text) @ \(Int(r.minX)),\(Int(r.minY)) \(Int(r.width))x\(Int(r.height)) \(role(hit.element))")
    } else if press(hit) {
        print("yes \(hit.text)")
    } else {
        print("no press \(hit.text)")
    }
    exit(0)
}

guard let front = frontWindow() else {
    FileHandle.standardError.write("no-windows\n".data(using: .utf8)!)
    exit(4)
}

var web = findWebArea(front)
if web == nil { web = findWebArea(axApp) }        // some apps hang content off the app
guard let web = web else {
    FileHandle.standardError.write("no-web-area\n".data(using: .utf8)!)
    exit(5)
}

if mode == "probe" {
    var lines: [String] = []
    text(of: web, into: &lines)
    print("web-area-reachable lines=\(lines.count)")
    exit(0)
}

var lines: [String] = []
text(of: web, into: &lines)
print(lines.joined(separator: "\n"))
