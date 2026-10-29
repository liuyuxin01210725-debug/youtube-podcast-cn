import Foundation

/// Reads the iCloud Drive folder the Mac publisher fills: 「中文播客私人库」.
/// The folder is chosen once and kept with a security-scoped bookmark. Playback
/// progress stays on this phone.
///
/// Documents/中文播客私人库 inside the app container is a second, read-only
/// source: the Mac copies episodes there over USB when the phone is plugged in.
/// It is merged by episode id with the iCloud folder and never replaces it, so
/// a stale USB copy cannot hide what the Mac publishes to iCloud.
@MainActor
final class LibraryStore: ObservableObject {
    struct ListenMark: Codable {
        var seconds: Double
        var finished: Bool
    }

    @Published private(set) var episodes: [Episode] = []
    @Published private(set) var inbox: [InboxEntry] = []
    /// Something to show: the iCloud folder, or at least the USB copy.
    @Published private(set) var hasFolder = false
    /// The chosen iCloud folder is in use. False means new Mac publishes only arrive over USB.
    @Published private(set) var hasCloudFolder = false
    /// iCloud is still bringing down library.json or audio; the list polls faster meanwhile.
    @Published private(set) var waitingOnCloud = false
    @Published var message: String?
    @Published private var progress: [String: ListenMark] = [:]

    private let bookmarkKey = "PrivateListen.libraryBookmark"
    private let progressKey = "PrivateListen.progress"
    private let pendingKey = "PrivateListen.pendingShares"
    private var pendingShares: [PendingShare] = []
    /// Only ever the user-chosen iCloud folder, never the Documents copy.
    private var folderURL: URL?
    private var scopeHeld = false
    private let folderName = "中文播客私人库"
    private let unreadableMessage = "打不开资料库，请重新选择「中文播客私人库」。"

    var readingLocalCopyOnly: Bool { hasFolder && !hasCloudFolder }

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

    func acceptShare(url: URL, sourceApp: String?, contentType: String? = nil, expectedSpeakers: Int = 2) {
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
            try InboxDrop.write(url: url, sourceApp: sourceApp, into: folderURL, expectedSpeakers: expectedSpeakers, contentType: contentType)
            if contentType == "article" {
                message = "文章链接已放入收件箱。Mac 会抽正文入队（默认不立刻整篇 TTS）。"
            } else {
                message = "已放入收件箱。Mac 上的监听会开始转换。"
            }
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
        Self.isUnplayed(progress[id])
    }

    func isFinished(_ id: String) -> Bool {
        progress[id]?.finished == true
    }

    /// Partial listen only. Finished episodes start over next time; the row says 已听完.
    func continuePointLabel(for id: String) -> String? {
        guard let entry = progress[id], !entry.finished, entry.seconds >= 5 else { return nil }
        return "上次听到 \(listenClock(entry.seconds))"
    }

    func playedFraction(for episode: Episode) -> Double? {
        guard let entry = progress[episode.id], !entry.finished, entry.seconds >= 5 else { return nil }
        let duration = episode.durationSec ?? 0
        guard duration > 1 else { return nil }
        return min(1, max(0, entry.seconds / duration))
    }

    func isFinished(_ id: String) -> Bool {
        progress[id]?.finished == true
    }

    func resumeSeconds(for id: String) -> Double {
        Self.resumeSeconds(in: progress, id: id)
    }

    func remember(id: String, seconds: Double, duration: Double) {
        guard let next = Self.reducing(progress, id: id, seconds: seconds, duration: duration) else { return }
        progress = next
        if let data = try? JSONEncoder().encode(progress) {
            UserDefaults.standard.set(data, forKey: progressKey)
        }
    }

    nonisolated static func isUnplayed(_ entry: ListenMark?) -> Bool {
        guard let entry else { return true }
        return entry.seconds < 5 && !entry.finished
    }

    /// Per-episode map. A new id never replaces another. Near the end counts as finished,
    /// and a finished episode resumes from the start.
    nonisolated static func reducing(_ progress: [String: ListenMark], id: String, seconds: Double, duration: Double) -> [String: ListenMark]? {
        guard seconds.isFinite, seconds >= 0, !id.isEmpty else { return nil }
        let finished = duration > 1 && seconds >= max(0, duration - 15)
        let mark = ListenMark(seconds: seconds, finished: finished)
        if progress[id]?.seconds == mark.seconds && progress[id]?.finished == mark.finished { return nil }
        var next = progress
        next[id] = mark
        return next
    }

    nonisolated static func resumeSeconds(in progress: [String: ListenMark], id: String) -> Double {
        guard let entry = progress[id], !entry.finished else { return 0 }
        return max(0, entry.seconds)
    }

    func adopt(_ url: URL) {
        if Self.isInsideAppContainer(url) {
            message = "这是 App 里的本机副本。请在 iCloud 云盘里选「中文播客私人库」。"
            return
        }
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
        var cloud = LoadedLibrary()
        var cloudInbox: [InboxEntry] = []
        if let folderURL {
            let readable = FileManager.default.isReadableFile(atPath: folderURL.path)
            if !readable && !scopeHeld {
                message = unreadableMessage
            } else {
                flushPending()
                cloud = loadEpisodes(from: folderURL, localCopy: false)
                cloudInbox = InboxDrop.items(in: folderURL)
                if message == unreadableMessage {
                    message = nil
                }
            }
        }
        let mirrorURL = documentsLibraryURL()
        let mirror = mirrorURL.map { loadEpisodes(from: $0, localCopy: true) } ?? LoadedLibrary()
        episodes = merge(cloud: cloud.episodes, localCopy: mirror.episodes)
        inbox = cloudInbox
        hasCloudFolder = folderURL != nil
        hasFolder = folderURL != nil || mirrorURL != nil
        waitingOnCloud = cloud.waiting || episodes.contains { $0.downloading }
    }

    /// iCloud wins. The USB copy fills in episodes iCloud has not delivered yet,
    /// or whose audio is still downloading from iCloud.
    private func merge(cloud: [Episode], localCopy: [Episode]) -> [Episode] {
        var byID: [String: Episode] = [:]
        for episode in cloud {
            byID[episode.id] = episode
        }
        for episode in localCopy {
            if let existing = byID[episode.id], !existing.downloading { continue }
            byID[episode.id] = episode
        }
        return byID.values.sorted { lhs, rhs in
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt > rhs.createdAt }
            return lhs.id > rhs.id
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

    /// Picks the iCloud folder only. The Documents copy is read separately in
    /// `reload`; holding it here used to make the app look set up while it never
    /// saw an iCloud publish again.
    private func restore() {
        if let data = UserDefaults.standard.data(forKey: bookmarkKey) {
            var stale = false
            do {
                let url = try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
                if Self.isInsideAppContainer(url) {
                    UserDefaults.standard.removeObject(forKey: bookmarkKey)
                } else {
                    hold(url)
                    if stale, let fresh = try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
                        UserDefaults.standard.set(fresh, forKey: bookmarkKey)
                    }
                    return
                }
            } catch {
                message = "iCloud 资料库的授权失效了，请点「选择资料库」重新选中「中文播客私人库」。"
            }
        }
        if let known = knownLibraryURL() {
            hold(known)
            return
        }
        release()
    }

    private static func isInsideAppContainer(_ url: URL) -> Bool {
        let home = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .resolvingSymlinksInPath().standardizedFileURL.path
        let path = url.resolvingSymlinksInPath().standardizedFileURL.path
        return path == home || path.hasPrefix(home + "/")
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

    /// Same folder name under Documents, filled by `studio_push_iphone.py` /
    /// `devicectl device copy to` when the phone is plugged into the Mac.
    private func documentsLibraryURL() -> URL? {
        guard let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
            return nil
        }
        let candidate = docs.appendingPathComponent(folderName, isDirectory: true)
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: candidate.path, isDirectory: &directory), directory.boolValue else {
            return nil
        }
        let library = candidate.appendingPathComponent("library.json")
        let episodes = candidate.appendingPathComponent("episodes", isDirectory: true)
        guard FileManager.default.fileExists(atPath: library.path)
            || FileManager.default.fileExists(atPath: episodes.path) else {
            return nil
        }
        return candidate
    }

    private func hold(_ url: URL) {
        release()
        folderURL = url
        scopeHeld = url.startAccessingSecurityScopedResource()
    }

    private func release() {
        if scopeHeld, let current = folderURL {
            current.stopAccessingSecurityScopedResource()
        }
        folderURL = nil
        scopeHeld = false
    }

    /// `localCopy` is the USB copy in Documents: nothing to download there, and an
    /// entry whose mp3 never arrived is skipped instead of shown as downloading.
    private func loadEpisodes(from root: URL, localCopy: Bool) -> LoadedLibrary {
        let fileManager = FileManager.default
        let episodesRoot = root.appendingPathComponent("episodes", isDirectory: true)
        let libraryURL = root.appendingPathComponent("library.json")
        var waiting = false
        if !localCopy {
            // The Mac replaces library.json on every publish; ask iCloud for the new version.
            try? fileManager.startDownloadingUbiquitousItem(at: libraryURL)
            try? fileManager.startDownloadingUbiquitousItem(at: episodesRoot)
            waiting = CloudFile.isWaiting(libraryURL)
        }
        var listed: Set<String> = []
        var records: [String: EpisodeRecord] = [:]
        if let library = decode(EpisodeLibrary.self, from: libraryURL) {
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
                listed.insert(id)
            }
        }
        let episodes = records.compactMap { pair -> Episode? in
            let id = pair.key
            let record = pair.value
            let folder = episodesRoot.appendingPathComponent(id, isDirectory: true)
            let audioURL = folder.appendingPathComponent("output.mp3")
            let local = LocalAudio.isPlayable(audioURL)
            if localCopy && !local { return nil }
            if !local {
                if !listed.contains(id) {
                    // Listed in library.json before iCloud has shown us the folder itself.
                    try? fileManager.startDownloadingUbiquitousItem(at: folder)
                }
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
        return LoadedLibrary(episodes: episodes, waiting: waiting)
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

private struct LoadedLibrary {
    var episodes: [Episode] = []
    var waiting = false
}

private enum CloudFile {
    /// True only for an iCloud item that exists but is not the current local version.
    static func isWaiting(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.ubiquitousItemDownloadingStatusKey]),
              let status = values.ubiquitousItemDownloadingStatus else {
            return false
        }
        return status != .current
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
