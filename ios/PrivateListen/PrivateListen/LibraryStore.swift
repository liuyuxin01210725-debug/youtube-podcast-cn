import Foundation

/// Reads the iCloud Drive folder the Mac publisher fills: 「中文播客私人库」.
/// The folder is chosen once and kept with a security-scoped bookmark. Playback
/// progress stays on this phone.
@MainActor
final class LibraryStore: ObservableObject {
    struct ListenMark: Codable {
        var seconds: Double
        var finished: Bool
    }

    @Published private(set) var episodes: [Episode] = []
    @Published private(set) var inbox: [InboxEntry] = []
    @Published private(set) var hasFolder = false
    @Published var message: String?
    @Published private var progress: [String: ListenMark] = [:]

    private let bookmarkKey = "PrivateListen.libraryBookmark"
    private let progressKey = "PrivateListen.progress"
    private let pendingKey = "PrivateListen.pendingShares"
    private var pendingShares: [PendingShare] = []
    private var folderURL: URL?
    private var scopeHeld = false
    private let folderName = "中文播客私人库"

    init() {
        if let data = UserDefaults.standard.data(forKey: progressKey),
           let stored = try? JSONDecoder().decode([String: ListenMark].self, from: data) {
            progress = stored
        }
        if let data = UserDefaults.standard.data(forKey: pendingKey),
           let stored = try? JSONDecoder().decode([PendingShare].self, from: data) {
            pendingShares = stored
        }
        restore()
        reload()
    }

    func acceptShare(url: URL, sourceApp: String?) {
        guard InboxDrop.accepts(url) else {
            message = "这条不是公开的 HTTPS 链接。"
            return
        }
        guard let folderURL else {
            pendingShares.append(PendingShare(url: url.absoluteString, sourceApp: sourceApp, sharedAt: Date()))
            savePending()
            message = "先选择「中文播客私人库」，这条链接会放进收件箱。"
            return
        }
        do {
            try InboxDrop.write(url: url, sourceApp: sourceApp, into: folderURL)
            message = "已放入收件箱。Mac 上的监听会开始转换。"
            reload()
        } catch {
            message = "没能写入收件箱，请重新选择资料库。"
        }
    }

    func acceptIncoming(_ url: URL) {
        guard url.scheme?.lowercased() == "privatelisten" else { return }
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        guard let shared = components?.queryItems?.first(where: { $0.name == "url" })?.value,
              let target = URL(string: shared) else {
            message = "分享里没有链接。"
            return
        }
        let source = components?.queryItems?.first(where: { $0.name == "source_app" })?.value
        acceptShare(url: target, sourceApp: source)
    }

    func isUnplayed(_ id: String) -> Bool {
        guard let entry = progress[id] else { return true }
        return entry.seconds < 5 && !entry.finished
    }

    func resumeSeconds(for id: String) -> Double {
        guard let entry = progress[id], !entry.finished else { return 0 }
        return max(0, entry.seconds)
    }

    func remember(id: String, seconds: Double, duration: Double) {
        guard seconds.isFinite, seconds >= 0 else { return }
        let finished = duration > 1 && seconds >= max(0, duration - 15)
        let mark = ListenMark(seconds: seconds, finished: finished)
        if progress[id]?.seconds == mark.seconds && progress[id]?.finished == mark.finished { return }
        progress[id] = mark
        if let data = try? JSONEncoder().encode(progress) {
            UserDefaults.standard.set(data, forKey: progressKey)
        }
    }

    func adopt(_ url: URL) {
        let started = url.startAccessingSecurityScopedResource()
        do {
            let data = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
            UserDefaults.standard.set(data, forKey: bookmarkKey)
        } catch {
            if started { url.stopAccessingSecurityScopedResource() }
            message = "无法记住这个文件夹，请再选一次「中文播客私人库」。"
            return
        }
        if started { url.stopAccessingSecurityScopedResource() }
        message = nil
        restore()
        reload()
    }

    func reload() {
        guard let folderURL else {
            hasFolder = false
            episodes = []
            inbox = []
            return
        }
        hasFolder = true
        let readable = FileManager.default.isReadableFile(atPath: folderURL.path)
        if !readable && !scopeHeld {
            episodes = []
            inbox = []
            message = "打不开资料库，请重新选择「中文播客私人库」。"
            return
        }
        flushPending()
        episodes = loadEpisodes(from: folderURL)
        inbox = InboxDrop.items(in: folderURL)
        if message == "打不开资料库，请重新选择「中文播客私人库」。" {
            message = nil
        }
    }

    private func flushPending() {
        guard let folderURL, !pendingShares.isEmpty else { return }
        var left: [PendingShare] = []
        for item in pendingShares {
            guard let url = URL(string: item.url) else { continue }
            do {
                try InboxDrop.write(url: url, sourceApp: item.sourceApp, into: folderURL)
            } catch {
                left.append(item)
            }
        }
        pendingShares = left
        savePending()
        if left.isEmpty {
            message = "已放入收件箱。Mac 上的监听会开始转换。"
        }
    }

    private func savePending() {
        if let data = try? JSONEncoder().encode(pendingShares) {
            UserDefaults.standard.set(data, forKey: pendingKey)
        }
    }

    private func restore() {
        if let data = UserDefaults.standard.data(forKey: bookmarkKey) {
            var stale = false
            if let url = try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale) {
                if stale, let fresh = try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
                    UserDefaults.standard.set(fresh, forKey: bookmarkKey)
                }
                hold(url)
                return
            }
        }
        if let known = knownLibraryURL() {
            hold(known)
        }
    }

    /// Sandbox hides this path on iPhone. It works if the process can already see iCloud Drive.
    private func knownLibraryURL() -> URL? {
        let candidate = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs/\(folderName)", isDirectory: true)
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: candidate.path, isDirectory: &directory), directory.boolValue else {
            return nil
        }
        return candidate
    }

    private func hold(_ url: URL) {
        if scopeHeld, let current = folderURL {
            current.stopAccessingSecurityScopedResource()
        }
        folderURL = url
        scopeHeld = url.startAccessingSecurityScopedResource()
        hasFolder = true
    }

    private func loadEpisodes(from root: URL) -> [Episode] {
        let fileManager = FileManager.default
        let episodesRoot = root.appendingPathComponent("episodes", isDirectory: true)
        try? fileManager.startDownloadingUbiquitousItem(at: episodesRoot)
        var records: [String: EpisodeRecord] = [:]
        if let library = decode(EpisodeLibrary.self, from: root.appendingPathComponent("library.json")) {
            for record in library.episodes {
                guard let id = record.id, EpisodeID.isSafe(id) else { continue }
                records[id] = record
            }
        }
        let keys: Set<URLResourceKey> = [.isDirectoryKey]
        if let children = try? fileManager.contentsOfDirectory(at: episodesRoot, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles]) {
            for child in children {
                let id = child.lastPathComponent
                guard EpisodeID.isSafe(id) else { continue }
                let isDirectory = (try? child.resourceValues(forKeys: keys).isDirectory) ?? false
                guard isDirectory else { continue }
                guard var record = decode(EpisodeRecord.self, from: child.appendingPathComponent("episode.json")) else { continue }
                record.id = id
                if record.title?.isEmpty != false {
                    record.title = records[id]?.title
                }
                records[id] = record
            }
        }
        return records.compactMap { pair -> Episode? in
            let id = pair.key
            let record = pair.value
            let folder = episodesRoot.appendingPathComponent(id, isDirectory: true)
            let audioURL = folder.appendingPathComponent("output.mp3")
            let local = LocalAudio.isPlayable(audioURL)
            if !local {
                try? fileManager.startDownloadingUbiquitousItem(at: audioURL)
            }
            let title = record.title?.trimmingCharacters(in: .whitespacesAndNewlines)
            let artworkFile = EpisodeArtwork.file(record.artwork, kind: record.artworkKind)
            if let artworkFile, record.artworkKind != "placeholder" {
                let artworkURL = folder.appendingPathComponent(artworkFile)
                if !LocalAudio.isPlayable(artworkURL) {
                    try? fileManager.startDownloadingUbiquitousItem(at: artworkURL)
                }
            }
            return Episode(
                id: id,
                title: (title?.isEmpty == false ? title! : id),
                createdAt: EpisodeDate.parse(record.createdAt) ?? Date(timeIntervalSince1970: 0),
                durationSec: record.durationSec,
                audioURL: audioURL,
                downloading: !local,
                artworkFile: artworkFile,
                artworkKind: record.artworkKind
            )
        }
        .sorted { lhs, rhs in
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt > rhs.createdAt }
            return lhs.id > rhs.id
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from url: URL) -> T? {
        guard let data = readData(url) else { return nil }
        return try? JSONDecoder.privateListen.decode(type, from: data)
    }

    private func readData(_ url: URL) -> Data? {
        let coordinator = NSFileCoordinator()
        var result: Data?
        var error: NSError?
        coordinator.coordinate(readingItemAt: url, options: [], error: &error) { readURL in
            result = try? Data(contentsOf: readURL)
        }
        if result == nil {
            result = try? Data(contentsOf: url)
        }
        return result
    }
}

private struct EpisodeLibrary: Decodable {
    var episodes: [EpisodeRecord]
}

private struct EpisodeRecord: Decodable {
    var id: String?
    var title: String?
    var createdAt: String?
    var durationSec: Double?
    var file: String?
    var youtubeId: String?
    var artwork: String?
    var artworkKind: String?
}

private struct PendingShare: Codable {
    var url: String
    var sourceApp: String?
    var sharedAt: Date
}

private enum EpisodeArtwork {
    static func file(_ name: String?, kind: String?) -> String? {
        guard let name, ["cover.jpg", "cover.png", "cover.webp"].contains(name) else { return nil }
        guard kind == nil || ["provided", "thumbnail", "title", "placeholder"].contains(kind) else { return nil }
        return name
    }
}

private extension JSONDecoder {
    static let privateListen: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
