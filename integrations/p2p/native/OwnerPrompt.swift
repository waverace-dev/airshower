import AppKit

// One short-lived, native owner decision. No network, model, shell or chat
// automation. Input and the chosen result travel only over private stdio pipes.
func finish(_ value: [String: String]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
    exit(0)
}
if CommandLine.arguments == [CommandLine.arguments[0], "--version"] {
    print("Airshower owner prompt 0.4.0")
    exit(0)
}
var input = Data()
while let chunk = try? FileHandle.standardInput.read(upToCount: 8192), !chunk.isEmpty {
    input.append(chunk)
    if input.count > 128 * 1024 { finish(["action": "cancel"]) }
}
guard let prompt = (try? JSONSerialization.jsonObject(with: input)) as? [String: String],
      Set(prompt.keys) == Set(["kind", "title", "detail", "accept"]),
      let kind = prompt["kind"], ["confirm", "request", "answer", "file"].contains(kind),
      let title = prompt["title"], title.utf8.count <= 512,
      let detail = prompt["detail"], detail.utf8.count <= 100 * 1024,
      let accept = prompt["accept"], accept.utf8.count <= 80 else { finish(["action": "cancel"]) }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let alert = NSAlert()
alert.alertStyle = .informational
alert.messageText = title
alert.informativeText = "Airshower · Your decision stays in your control."
alert.addButton(withTitle: "Cancel").keyEquivalent = "\u{1b}"
alert.addButton(withTitle: accept).keyEquivalent = ""
if kind == "request" || kind == "answer" { alert.addButton(withTitle: "Decline").keyEquivalent = "" }
alert.window.defaultButtonCell = nil

let width: CGFloat = 540
func textArea(_ height: CGFloat, editable: Bool, value: String) -> (NSScrollView, NSTextView) {
    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: width, height: height))
    scroll.hasVerticalScroller = true
    scroll.borderType = .bezelBorder
    let text = NSTextView(frame: scroll.contentView.bounds)
    text.isEditable = editable
    text.isSelectable = true
    text.isRichText = false
    text.font = NSFont.systemFont(ofSize: 13)
    text.textColor = .labelColor
    text.backgroundColor = .textBackgroundColor
    text.textContainerInset = NSSize(width: 12, height: 12)
    text.isVerticallyResizable = true
    text.isHorizontallyResizable = false
    text.autoresizingMask = [.width]
    text.textContainer?.widthTracksTextView = true
    text.textContainer?.containerSize = NSSize(width: width - 24, height: .greatestFiniteMagnitude)
    text.string = value
    text.setAccessibilityLabel(editable ? "Your reply" : "Request details")
    scroll.documentView = text
    return (scroll, text)
}
let stack = NSStackView()
stack.orientation = .vertical
stack.alignment = .leading
stack.spacing = 12
let (details, _) = textArea(260, editable: false, value: detail)
stack.addArrangedSubview(details)
details.widthAnchor.constraint(equalToConstant: width).isActive = true
details.heightAnchor.constraint(equalToConstant: 260).isActive = true
var reply: NSTextView?
if kind == "answer" {
    let label = NSTextField(labelWithString: "Your answer")
    stack.addArrangedSubview(label)
    let (scroll, field) = textArea(90, editable: true, value: "")
    reply = field
    stack.addArrangedSubview(scroll)
    scroll.widthAnchor.constraint(equalToConstant: width).isActive = true
    scroll.heightAnchor.constraint(equalToConstant: 90).isActive = true
}
stack.frame = NSRect(x: 0, y: 0, width: width, height: kind == "answer" ? 390 : 260)
alert.accessoryView = stack
app.activate(ignoringOtherApps: true)
let result = alert.runModal()
if result == .alertThirdButtonReturn { finish(["action": "deny"]) }
if result != .alertSecondButtonReturn { finish(["action": "cancel"]) }
if kind == "answer" {
    let text = reply?.string ?? ""
    if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.utf8.count > 16384 { finish(["action": "cancel"]) }
    finish(["action": "answer", "text": text])
}
if kind == "file" {
    let panel = NSOpenPanel()
    panel.title = title
    panel.message = "Choose the exact file this contact may request. Maximum 5 MiB."
    panel.prompt = "Publish this file"
    panel.canChooseFiles = true
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    if panel.runModal() == .OK, let url = panel.url { finish(["action": "approve", "path": url.path]) }
    finish(["action": "cancel"])
}
finish(["action": "approve"])
