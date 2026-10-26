import UIKit
import UniformTypeIdentifiers

/// Share sheet target. Personal Team has no iCloud container and no App Group,
/// so this extension keeps its own security-scoped bookmark of 「中文播客私人库」.
@MainActor
final class ShareViewController: UIViewController, UIDocumentPickerDelegate {
    private let label = UILabel()
    private let chooseButton = UIButton(type: .system)
    private let openButton = UIButton(type: .system)
    private let cancelButton = UIButton(type: .system)
    private var pending: URL?
    private var finished = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        preferredContentSize = CGSize(width: 320, height: 220)
        label.numberOfLines = 0
        label.textAlignment = .center
        label.font = .preferredFont(forTextStyle: .body)
        label.text = "正在收取链接…"
        chooseButton.setTitle("选择中文播客私人库", for: .normal)
        chooseButton.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        chooseButton.addTarget(self, action: #selector(pickFolder), for: .touchUpInside)
        openButton.setTitle("打开私人听写入", for: .normal)
        openButton.addTarget(self, action: #selector(openHostTapped), for: .touchUpInside)
        cancelButton.setTitle("取消", for: .normal)
        cancelButton.addTarget(self, action: #selector(cancelTapped), for: .touchUpInside)
        let stack = UIStackView(arrangedSubviews: [label, chooseButton, openButton, cancelButton])
        stack.axis = .vertical
        stack.spacing = 16
        stack.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
        setFolderChoices(hidden: true)
        Task { await receive() }
    }

    @objc private func pickFolder() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        picker.delegate = self
        picker.allowsMultipleSelection = false
        present(picker, animated: true)
    }

    @objc private func openHostTapped() {
        guard let pending else { return }
        openHost(pending)
    }

    @objc private func cancelTapped() {
        guard !finished else { return }
        finished = true
        extensionContext?.cancelRequest(withError: NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError))
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {}

    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        guard let folder = urls.first, let pending else { return }
        let started = folder.startAccessingSecurityScopedResource()
        defer { if started { folder.stopAccessingSecurityScopedResource() } }
        guard started else {
            show("没能打开这个文件夹。请选 iCloud 云盘里的「中文播客私人库」。")
            return
        }
        do {
            try InboxDrop.write(url: pending, sourceApp: nil, into: folder)
            try ShareBookmark.save(folder)
            finish("已放入收件箱")
        } catch {
            show("没能写入这个文件夹。请选 iCloud 云盘里的「中文播客私人库」。")
        }
    }

    private func receive() async {
        guard let url = await incomingURL() else {
            show("没有可用的链接")
            return
        }
        if let folder = ShareBookmark.resolve() {
            defer { folder.stopAccessingSecurityScopedResource() }
            do {
                try InboxDrop.write(url: url, sourceApp: nil, into: folder)
                finish("已放入收件箱")
                return
            } catch {
                ShareBookmark.clear()
            }
        }
        pending = url
        show("第一次请选 iCloud 云盘里的「中文播客私人库」。选过一次后，分享会直接写入收件箱。")
        setFolderChoices(hidden: false)
    }

    private func incomingURL() async -> URL? {
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        for item in items {
            for provider in item.attachments ?? [] {
                if let url = await loadURL(from: provider) { return url }
            }
        }
        return nil
    }

    private func loadURL(from provider: NSItemProvider) async -> URL? {
        if provider.canLoadObject(ofClass: URL.self) {
            let loaded: URL? = await withCheckedContinuation { continuation in
                provider.loadObject(ofClass: URL.self) { object, _ in
                    continuation.resume(returning: object as? URL)
                }
            }
            if let loaded, InboxDrop.accepts(loaded) { return loaded }
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            let text: String? = await withCheckedContinuation { continuation in
                provider.loadItem(forTypeIdentifier: UTType.plainText.identifier, options: nil) { item, _ in
                    if let text = item as? String {
                        continuation.resume(returning: text)
                    } else if let data = item as? Data {
                        continuation.resume(returning: String(data: data, encoding: .utf8))
                    } else {
                        continuation.resume(returning: nil)
                    }
                }
            }
            if let text, let url = InboxDrop.httpsURL(in: text) { return url }
        }
        return nil
    }

    private func openHost(_ url: URL) {
        var components = URLComponents()
        components.scheme = "privatelisten"
        components.host = "share"
        components.queryItems = [URLQueryItem(name: "url", value: url.absoluteString)]
        guard let destination = components.url else { return }
        extensionContext?.open(destination) { success in
            DispatchQueue.main.async {
                if success || self.openViaResponder(destination) {
                    self.finish("已打开私人听")
                } else {
                    self.show("没能打开私人听。请改用「选择中文播客私人库」。")
                }
            }
        }
    }

    private func openViaResponder(_ url: URL) -> Bool {
        var responder: UIResponder? = self
        let selector = sel_registerName("openURL:")
        while let current = responder {
            if current.responds(to: selector) {
                _ = current.perform(selector, with: url)
                return true
            }
            responder = current.next
        }
        return false
    }

    private func show(_ text: String) {
        label.text = text
    }

    private func setFolderChoices(hidden: Bool) {
        chooseButton.isHidden = hidden
        openButton.isHidden = hidden
        cancelButton.isHidden = hidden
    }

    private func finish(_ text: String) {
        guard !finished else { return }
        finished = true
        label.text = text
        setFolderChoices(hidden: true)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 600_000_000)
            self.extensionContext?.completeRequest(returningItems: nil)
        }
    }
}

private enum ShareBookmark {
    static let key = "PrivateListen.shareLibraryBookmark"

    static func save(_ url: URL) throws {
        let data = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
        UserDefaults.standard.set(data, forKey: key)
    }

    static func clear() {
        UserDefaults.standard.removeObject(forKey: key)
    }

    static func resolve() -> URL? {
        guard let data = UserDefaults.standard.data(forKey: key) else { return nil }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale) else {
            return nil
        }
        if stale, let fresh = try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
            UserDefaults.standard.set(fresh, forKey: key)
        }
        guard url.startAccessingSecurityScopedResource() else { return nil }
        return url
    }
}
