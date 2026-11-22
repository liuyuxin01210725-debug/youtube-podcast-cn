import Foundation
import Combine

/// Reads the iCloud Drive folder the Mac publisher fills: 「中文播客私人库」.
/// The folder is chosen once and kept with a security-scoped bookmark. Playback
/// progress stays on this phone.
///
/// The selected iCloud catalog is authoritative. With no iCloud folder, USB's
/// Documents copy has its own catalog. A reload never combines their rows.
@MainActor
final class LibraryStore: ObservableObject {
    struct ListenMark: Codable, Equatable {
        enum FinishedSource: String, Codable, Equatable {
            case unknown
            case playback
            case manual
        }

        var seconds: Double
        var finished: Bool
        /// Missing in records written before this field existed. Load does not rewrite it.
        var finishedSource: FinishedSource? = nil
        /// Playhead for a finished episode that is being replayed. Missing means start over.
        var replaySeconds: Double? = nil
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
    @Published private var continueDismissedIDs: Set<String> = []
    @Published private var continueTouched: [String: TimeInterval] = [:]

    private let bookmarkKey = "PrivateListen.libraryBookmark"
    private let progressKey = "PrivateListen.progress"
    private let pendingKey = "PrivateListen.pendingShares"
    /// Ids removed from 「继续播放」 by swipe. ListenMark seconds are kept.
    private let dismissedKey = "PrivateListen.continueDismissed"
    /// Last ListenMark write time, so the rail can sort by recent listens.
    private let touchedKey = "PrivateListen.continueTouched"
    private var pendingShares: [PendingShare] = []
    /// Only ever the user-chosen iCloud folder, never the Documents copy.
    private var folderURL: URL?
    private var scopeHeld = false
    private let folderName = "中文播客私人库"
    private let unreadableMessage = "打不开资料库，请重新选择「中文播客私人库」。"

    var readingLocalCopyOnly: Bool { hasFolder && !hasCloudFolder }

    private let revisionVerifier = RevisionAssetVerifier()
    private var lastSnapshot: (root: URL, episodes: [Episode])?
    private var visibleByID: [String: Episode] = [:]

    private let defaults: UserDefaults
    private let documentsDirectory: URL?
    /// In-process ingest lock. Must not live in UserDefaults: a crash would stick it.
    private var ingestBusy = false

    init(defaults: UserDefaults = .standard, loadLibraryOnInit: Bool = true,
         documentsDirectory: URL? = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first) {
        self.defaults = defaults
        self.documentsDirectory = documentsDirectory
        if let data = defaults.data(forKey: progressKey),
           let stored = try? JSONDecoder().decode([String: ListenMark].self, from: data) {
            progress = stored
        }
        if let ids = defaults.array(forKey: dismissedKey) as? [String] {
            continueDismissedIDs = Set(ids)
        }
        if let data = defaults.data(forKey: touchedKey),
           let stored = try? JSONDecoder().decode([String: TimeInterval].self, from: data) {
            continueTouched = stored
        }
        if let data = defaults.data(forKey: pendingKey),
           let stored = try? JSONDecoder().decode([PendingShare].self, from: data) {
            pendingShares = stored
        }
        if loadLibraryOnInit {
            restore()
            reload()
        }
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

    private func mediaKey(for id: String) -> String { visibleByID[id]?.mediaKey ?? id }

    func isUnplayed(_ id: String) -> Bool {
        Self.isUnplayed(progress[mediaKey(for: id)])
    }

    func isFinished(_ id: String) -> Bool {
        progress[mediaKey(for: id)]?.finished == true
    }

    /// In progress: started (≥5s) and not finished.
    func isInProgress(_ id: String) -> Bool {
        guard let entry = progress[mediaKey(for: id)], !entry.finished else { return false }
        return entry.seconds >= 5
    }

    /// Label for an unfinished shelf row. Continue displays replay cursors separately.
    func continuePointLabel(for id: String) -> String? {
        guard let entry = progress[mediaKey(for: id)], !entry.finished, entry.seconds >= 5 else { return nil }
        return "上次听到 \(listenClock(entry.seconds))"
    }

    func playedFraction(for episode: Episode) -> Double? {
        guard let entry = progress[episode.mediaKey], !entry.finished, entry.seconds >= 5 else { return nil }
        let duration = episode.durationSec ?? 0
        guard duration > 1 else { return nil }
        return min(1, max(0, entry.seconds / duration))
    }

    func resumeSeconds(for id: String) -> Double {
        Self.resumeSeconds(in: progress, id: mediaKey(for: id))
    }

    func resumeSeconds(for episode: Episode) -> Double {
        Self.resumeSeconds(in: progress, id: episode.mediaKey)
    }

    func isFinished(_ episode: Episode) -> Bool {
        progress[episode.mediaKey]?.finished == true
    }

    /// Returns whether this sample changed the episode from unfinished to finished.
    @discardableResult
    func remember(id: String, seconds: Double, duration: Double, finished: Bool? = nil) -> Bool {
        remember(mediaKey: mediaKey(for: id), seconds: seconds, duration: duration, finished: finished)
    }

    @discardableResult
    func remember(episode: Episode, seconds: Double, duration: Double, finished: Bool? = nil) -> Bool {
        remember(mediaKey: episode.mediaKey, seconds: seconds, duration: duration, finished: finished)
    }

    private func remember(mediaKey: String, seconds: Double, duration: Double, finished: Bool?) -> Bool {
        guard seconds.isFinite, seconds >= 0, !mediaKey.isEmpty else { return false }
        let wasFinished = progress[mediaKey]?.finished == true
        if let next = Self.reducing(progress, id: mediaKey, seconds: seconds, duration: duration, finished: finished) {
            progress = next
            saveProgressMap()
        }
        continueTouched[mediaKey] = Date().timeIntervalSince1970
        saveContinueBookkeeping()
        return !wasFinished && progress[mediaKey]?.finished == true
    }

    /// Manual archive uses the same finished fact as playback, without inventing progress.
    func markFinished(_ id: String) {
        guard !id.isEmpty else { return }
        progress[mediaKey(for: id)] = ListenMark(
            seconds: progress[mediaKey(for: id)]?.seconds ?? 0,
            finished: true,
            finishedSource: .manual,
            replaySeconds: nil
        )
        saveProgressMap()
    }

    /// Storage-only undo of a finished mark. There is no library row control for this.
    /// Keeps the current playhead (replay cursor if one exists). Mid-play writes must
    /// not glue finished back on.
    func markUnfinished(_ id: String) {
        guard !id.isEmpty else { return }
        let previous = progress[mediaKey(for: id)]
        let seconds: Double
        if let cursor = previous?.replaySeconds, cursor.isFinite, cursor > 0 {
            seconds = cursor
        } else {
            seconds = previous?.seconds ?? 0
        }
        progress[mediaKey(for: id)] = ListenMark(
            seconds: seconds,
            finished: false,
            finishedSource: nil,
            replaySeconds: nil
        )
        saveProgressMap()
    }

    /// The 「继续播放」 card: newest unfinished playhead or saved replay cursor
    /// (a few seconds count; the 5s floor stays on `isInProgress`), capped by
    /// `ContinueListening.displayLimit` (1).
    func continueEntries(in episodes: [Episode]) -> [ContinueListening.Item] {
        var marks: [String: ContinueListening.Mark] = [:]
        var durations: [String: Double] = [:]
        for episode in episodes {
            guard let entry = progress[episode.mediaKey] else { continue }
            marks[episode.id] = ContinueListening.Mark(
                seconds: entry.seconds,
                finished: entry.finished,
                touchedAt: continueTouched[episode.mediaKey] ?? 0,
                replaySeconds: entry.replaySeconds
            )
            durations[episode.id] = episode.durationSec ?? 0
        }
        return ContinueListening.items(
            marks: marks,
            durations: durations,
            dismissed: Set(episodes.filter { continueDismissedIDs.contains($0.mediaKey) }.map(\.id))
        )
    }

    /// Left swipe on a continue row. Does not delete the episode or its ListenMark.
    func dismissContinue(_ id: String) {
        let next = ContinueListening.dismissing(continueDismissedIDs, id: mediaKey(for: id))
        guard next != continueDismissedIDs else { return }
        continueDismissedIDs = next
        saveContinueBookkeeping()
    }

    /// Opening the episode or pressing its continue pill puts the row back.
    func acknowledgeContinue(_ id: String) {
        acknowledgeContinue(mediaKey: mediaKey(for: id))
    }

    func acknowledgeContinue(_ episode: Episode) {
        acknowledgeContinue(mediaKey: episode.mediaKey)
    }

    private func acknowledgeContinue(mediaKey: String) {
        let next = ContinueListening.acknowledging(continueDismissedIDs, id: mediaKey)
        guard next != continueDismissedIDs else { return }
        continueDismissedIDs = next
        saveContinueBookkeeping()
    }

    private func saveProgressMap() {
        if let data = try? JSONEncoder().encode(progress) {
            defaults.set(data, forKey: progressKey)
        }
    }

    private func saveContinueBookkeeping() {
        defaults.set(Array(continueDismissedIDs), forKey: dismissedKey)
        if let data = try? JSONEncoder().encode(continueTouched) {
            defaults.set(data, forKey: touchedKey)
        }
    }

    nonisolated static func isUnplayed(_ entry: ListenMark?) -> Bool {
        guard let entry else { return true }
        return entry.seconds < 5 && !entry.finished
    }

    /// Ordinary transport reports natural EOF. Point listening keeps the near-end rule
    /// for unfinished episodes. A finished mark is never auto-demoted; mid-replay is
    /// stored as a cursor. Migration keeps existing seconds/finished and leaves source unknown.
    nonisolated static func reducing(_ progress: [String: ListenMark], id: String, seconds: Double, duration: Double, finished: Bool? = nil) -> [String: ListenMark]? {
        guard seconds.isFinite, seconds >= 0, !id.isEmpty else { return nil }
        let previous = progress[id]
        let nearEnd = duration > 1 && seconds >= max(0, duration - 15)
        let mark: ListenMark
        if previous?.finished == true {
            // Do not reuse the 15-second unfinished rule; an 8s replay at 0.45s
            // is mid-cursor, not a second completion.
            let reachedEndAgain = finished == true
                || (duration > 0 && seconds >= duration - (1.0 / 600))
            mark = ListenMark(
                seconds: seconds,
                finished: true,
                finishedSource: previous?.finishedSource,
                replaySeconds: reachedEndAgain || seconds <= 0 ? nil : seconds
            )
        } else if finished == true {
            mark = ListenMark(seconds: seconds, finished: true, finishedSource: .playback, replaySeconds: nil)
        } else if let finished {
            mark = ListenMark(seconds: seconds, finished: finished, finishedSource: nil, replaySeconds: nil)
        } else {
            mark = ListenMark(
                seconds: seconds,
                finished: nearEnd,
                finishedSource: nearEnd ? .playback : nil,
                replaySeconds: nil
            )
        }
        if previous == mark { return nil }
        var next = progress
        next[id] = mark
        return next
    }

    nonisolated static func resumeSeconds(in progress: [String: ListenMark], id: String) -> Double {
        guard let entry = progress[id] else { return 0 }
        if entry.finished {
            if let cursor = entry.replaySeconds, cursor.isFinite, cursor > 0 {
                return cursor
            }
            return 0
        }
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
            defaults.set(data, forKey: bookmarkKey)
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
        if let folderURL {
            if FileManager.default.isReadableFile(atPath: folderURL.path) || scopeHeld {
                flushPending()
                inbox = InboxDrop.items(in: folderURL)
                if message == unreadableMessage { message = nil }
            } else {
                message = unreadableMessage
                inbox = []
            }
        } else {
            inbox = []
        }
        let root = folderURL ?? documentsLibraryURL()
        hasCloudFolder = folderURL != nil
        hasFolder = root != nil
        guard let root else {
            lastSnapshot = nil
            visibleByID = [:]
            episodes = []
            waitingOnCloud = false
            return
        }

        let loaded = Self.loadEpisodes(from: root, localCopy: folderURL == nil, verifier: revisionVerifier)
        if loaded.accepted {
            lastSnapshot = (root, loaded.episodes)
        }
        let visible = lastSnapshot?.root == root ? lastSnapshot?.episodes ?? [] : []
        visibleByID = Dictionary(uniqueKeysWithValues: visible.map { ($0.id, $0) })
        episodes = visible
        waitingOnCloud = loaded.waiting || visible.contains { $0.downloading }
    }

    /// The Documents catalog switch is serialized with reads and other commits
    /// on the main actor. The caller supplies snapshot bytes and the expected
    /// digest of the existing file, after delivering immutable objects.
    /// A nil digest expects no existing file.
    @discardableResult
    func commitLocalSnapshot(_ data: Data, expecting expectedSHA256: String?) throws -> String {
        guard let root = documentsLibraryURL() else { throw LibrarySnapshotError.invalidPath }
        let digest = try Self.commitSnapshot(data, expecting: expectedSHA256, in: root)
        reload()
        return digest
    }

    private static func commitSnapshot(_ data: Data, expecting expectedSHA256: String?, in root: URL) throws -> String {
        let incoming = try LibrarySnapshot(data)
        let listing = try LibrarySnapshot.path("library.json", in: root)
        func catalogBytes() throws -> Data? {
            do {
                return try Data(contentsOf: listing)
            } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
                return nil
            }
        }
        let before = try catalogBytes()
        let current = try before.map(LibrarySnapshot.init)
        let proposed = try incoming.preservingExtensions(from: current)
        let snapshot = try LibrarySnapshot(proposed)
        let repeated = try current?.canonicalData() == proposed
        let actual = before.map(LibrarySnapshot.digest)
        guard repeated || actual == expectedSHA256 else { throw LibrarySnapshotError.staleCatalog }
        let resolved = try Self.resolve(snapshot, from: root, localCopy: true, verifier: RevisionAssetVerifier())
        for episode in resolved {
            if let reference = episode.pendingTranscript {
                _ = try PendingTranscript.read(id: episode.id, audioURL: episode.audioURL, reference: reference).get()
            }
        }
        guard try catalogBytes() == before else { throw LibrarySnapshotError.staleCatalog }
        if repeated, let actual { return actual }
        try proposed.write(to: listing, options: .atomic)
        return LibrarySnapshot.digest(proposed)
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
            defaults.set(data, forKey: pendingKey)
        }
    }

    /// Picks the iCloud folder only. The Documents copy is read separately in
    /// `reload`; holding it here used to make the app look set up while it never
    /// saw an iCloud publish again.
    private func restore() {
        if let data = defaults.data(forKey: bookmarkKey) {
            var stale = false
            do {
                let url = try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
                if Self.isInsideAppContainer(url) {
                    defaults.removeObject(forKey: bookmarkKey)
                } else {
                    hold(url)
                    if stale, let fresh = try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
                        defaults.set(fresh, forKey: bookmarkKey)
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
        guard let documentsDirectory else { return nil }
        let candidate = documentsDirectory.appendingPathComponent(folderName, isDirectory: true)
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: candidate.path, isDirectory: &directory), directory.boolValue else {
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

    static func loadEpisodes(from root: URL, localCopy: Bool) -> LoadedLibrary {
        loadEpisodes(from: root, localCopy: localCopy, verifier: RevisionAssetVerifier())
    }

    private static func loadEpisodes(from root: URL, localCopy: Bool, verifier: RevisionAssetVerifier) -> LoadedLibrary {
        do {
            let listing = try LibrarySnapshot.path("library.json", in: root)
            let fileManager = FileManager.default
            if !localCopy {
                try? fileManager.startDownloadingUbiquitousItem(at: listing)
                guard !CloudFile.isWaiting(listing) else { return LoadedLibrary(waiting: true) }
            }
            let bytes = !localCopy && fileManager.isUbiquitousItem(at: listing)
                ? readData(listing) : try? Data(contentsOf: listing)
            guard let data = bytes else { return LoadedLibrary(waiting: !localCopy) }
            let snapshot = try LibrarySnapshot(data)
            let episodes = try resolve(snapshot, from: root, localCopy: localCopy, verifier: verifier)
            return LoadedLibrary(episodes: episodes, accepted: true)
        } catch {
            return LoadedLibrary(waiting: !localCopy)
        }
    }

    private static func resolve(_ snapshot: LibrarySnapshot, from root: URL, localCopy: Bool,
                                verifier: RevisionAssetVerifier) throws -> [Episode] {
        let fileManager = FileManager.default
        return try snapshot.records.map { record -> Episode in
            guard let id = record.catalogID else { throw LibrarySnapshotError.invalidCatalog }
            let folder = try LibrarySnapshot.path(record.mediaRevision?.path ?? "episodes/\(id)", in: root, directory: true)
            let audioURL = try LibrarySnapshot.path("output.mp3", in: folder)
            if let revision = record.mediaRevision {
                try revision.validateAssets(in: folder, downloading: !localCopy, verifier: verifier)
            }
            let local = LocalAudio.isPlayable(audioURL)
            if !local {
                guard !localCopy && record.mediaRevision == nil else { throw LibrarySnapshotError.incompleteRevision }
                try? fileManager.startDownloadingUbiquitousItem(at: folder)
                try? fileManager.startDownloadingUbiquitousItem(at: audioURL)
            }
            let title = record.title?.trimmingCharacters(in: .whitespacesAndNewlines)
            let artworkFile = EpisodeArtwork.file(record.artwork, kind: record.artworkKind)
            if let artworkFile {
                if let revision = record.mediaRevision, revision.files[artworkFile] == nil {
                    throw LibrarySnapshotError.invalidCatalog
                }
                let artworkURL = try LibrarySnapshot.path(artworkFile, in: folder)
                if !localCopy && record.artworkKind != "placeholder" && !LocalAudio.isPlayable(artworkURL) {
                    try? fileManager.startDownloadingUbiquitousItem(at: artworkURL)
                }
            }
            let show = record.show?.trimmingCharacters(in: .whitespacesAndNewlines)
            let guests = (record.guests ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            let lensIds = (record.lensIds ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            return Episode(
                id: id,
                title: title.flatMap { $0.isEmpty ? nil : $0 } ?? id,
                show: show.flatMap { $0.isEmpty ? nil : $0 },
                guests: guests,
                lensIds: lensIds,
                createdAt: EpisodeDate.parse(record.createdAt) ?? Date(timeIntervalSince1970: 0),
                durationSec: record.durationSec,
                audioURL: audioURL,
                downloading: !local,
                artworkFile: artworkFile,
                artworkKind: record.artworkKind,
                pendingTranscript: record.pendingReference(for: id),
                mediaRevision: record.mediaRevision?.sha256,
                revisionFiles: record.mediaRevision.map { Set($0.files.keys) }
            )
        }.sorted { lhs, rhs in
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt > rhs.createdAt }
            return lhs.id > rhs.id
        }
    }

    private static func readData(_ url: URL) -> Data? {
        let coordinator = NSFileCoordinator()
        var result: Data?
        var error: NSError?
        coordinator.coordinate(readingItemAt: url, options: [], error: &error) { readURL in
            result = try? Data(contentsOf: readURL)
        }
        return error == nil ? result : nil
    }
}

struct LoadedLibrary {
    var episodes: [Episode] = []
    var waiting = false
    var accepted = false
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

struct EpisodeRecord: Decodable {
    var id: String?
    var episodeId: String?
    var sourceJobId: String?
    var catalogID: String? { id ?? episodeId ?? sourceJobId }
    var title: String?
    var show: String?
    var guests: [String]?
    var lensIds: [String]?
    var createdAt: String?
    var durationSec: Double?
    var file: String?
    var youtubeId: String?
    var artwork: String?
    var artworkKind: String?
    var language: String?
    var audioKind: String?
    var transcriptStatus: String?
    var status: String?
    var transcript: String?
    var manifest: String?
    var pendingUnits: Int?
    var mediaRevision: MediaRevision?

    fileprivate var declaresPendingTranscript: Bool {
        audioKind == "original_audio" || transcriptStatus == "asr_unreviewed"
            || status == "pending_review" || transcript == "transcript.pending.json"
    }

    func pendingReference(for id: String) -> PendingTranscriptReference? {
        guard declaresPendingTranscript else { return nil }
        guard self.id == id, language == "zh", audioKind == "original_audio",
              transcriptStatus == "asr_unreviewed", status == "pending_review",
              transcript == "transcript.pending.json", manifest == "manifest.json", file == "output.mp3",
              let durationSec, durationSec.isFinite, durationSec > 0,
              let pendingUnits, pendingUnits > 0 else { return .invalid }
        return .package(id: id, duration: durationSec, count: pendingUnits)
    }
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

extension JSONDecoder {
    static func privateListen() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}


extension LibraryStore {
    func refreshLocalLibraryOnActivation() {
        do {
            try consumePushStaging()
        } catch {
            recordActivationFailure("consume-push-staging", error: error)
        }
        do {
            try migrateLegacyLibrary()
        } catch {
            recordActivationFailure("migrate-legacy", error: error)
        }
    }

    private func recordActivationFailure(_ operation: String, error: Error) {
        let nsError = error as NSError
        var entry: [String: Any] = [
            "operation": operation,
            "occurred_at": ISO8601DateFormatter().string(from: Date()),
            "domain": nsError.domain,
            "code": nsError.code,
            "localized_description": nsError.localizedDescription,
        ]
        if let error = error as? LibrarySnapshotError {
            entry["error_case"] = String(describing: error)
        } else if let error = error as? PendingTranscript.ReadFailure {
            entry["error_case"] = String(describing: error)
        }
        do {
            guard let documentsDirectory else { throw LibrarySnapshotError.invalidPath }
            let url = documentsDirectory.appendingPathComponent(".library-activation-\(operation)-error.json")
            try LibrarySnapshot.json(entry).write(to: url, options: .atomic)
        } catch {
            NSLog("PrivateListen activation %@ failed [%@ %ld]: %@ (record write: %@)",
                  operation, nsError.domain, nsError.code, nsError.localizedDescription, error.localizedDescription)
        }
    }

    /// Serial ingest / migration / recovery. Synchronous on the main actor so a
    /// mid-function await cannot re-enter the coordinator. USB is not opened here.
    @discardableResult
    func consumePushStaging(fault: IngestFault = .none) throws -> [IngestReceipt] {
        try serialIngest {
            guard let documentsDirectory else { return [] }
            let stagingRoot = documentsDirectory.appendingPathComponent(PushStaging.stagingName, isDirectory: true)
            var names = (try? FileManager.default.contentsOfDirectory(atPath: stagingRoot.path)) ?? []
            names.sort()
            var receipts: [IngestReceipt] = []
            var firstFailure: Error?
            for name in names where EpisodeID.isSafe(name) {
                let ready = stagingRoot.appendingPathComponent(name).appendingPathComponent(PushStaging.readyName)
                guard FileManager.default.isReadableFile(atPath: ready.path) else { continue }
                do {
                    receipts.append(try ingest(txId: name, fault: fault))
                } catch {
                    // Leave refused transactions unacknowledged and process independent pushes.
                    if firstFailure == nil { firstFailure = error }
                }
            }
            if let firstFailure { throw firstFailure }
            return receipts
        }
    }

    @discardableResult
    func migrateLegacyLibrary(fault: IngestFault = .none) throws -> IngestReceipt? {
        try serialIngest {
            try migrateLegacyLibraryLocked(fault: fault)
        }
    }

    func rebuildReceipt(txId: String) throws -> IngestReceipt {
        try serialIngest {
            try receiptFromLedger(txId: txId)
        }
    }

    private func serialIngest<T>(_ body: () throws -> T) throws -> T {
        // In-process reentrancy would interleave read/merge/commit. Transfer
        // by failing closed rather than starting a second writer.
        guard !ingestBusy else { throw LibrarySnapshotError.staleCatalog }
        ingestBusy = true
        defer { ingestBusy = false }
        guard let documentsDirectory else { throw LibrarySnapshotError.invalidPath }
        let root = documentsDirectory.appendingPathComponent("中文播客私人库", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        #if os(macOS) && PRIVATE_LISTEN_OFFLINE_PROBE
        // The sandboxed command-line probe cannot reach filecoordinationd.
        // Keep the main-actor gate and exercise real reads, merges and commits.
        return try body()
        #else
        var coordinated: Result<T, Error>?
        var error: NSError?
        NSFileCoordinator().coordinate(writingItemAt: root, options: [], error: &error) { _ in
            do {
                coordinated = .success(try body())
            } catch {
                coordinated = .failure(error)
            }
        }
        if let error { throw error }
        switch coordinated {
        case .success(let value): return value
        case .failure(let failure): throw failure
        case nil: throw LibrarySnapshotError.rejectedIngest
        }
        #endif
    }

    private func ingest(txId: String, fault: IngestFault) throws -> IngestReceipt {
        guard let documentsDirectory else { throw LibrarySnapshotError.invalidPath }
        let fm = FileManager.default
        let source = documentsDirectory.appendingPathComponent(PushStaging.stagingName, isDirectory: true)
            .appendingPathComponent(txId, isDirectory: true)
        let privateRoot = documentsDirectory.appendingPathComponent(PushStaging.privateName, isDirectory: true)
        let privateTx = privateRoot.appendingPathComponent(txId, isDirectory: true)
        if fm.fileExists(atPath: privateTx.path) {
            try fm.removeItem(at: privateTx)
        }
        try rejectLinkedPayloads(in: source)
        try fm.createDirectory(at: privateRoot, withIntermediateDirectories: true)
        try copyTree(from: source, to: privateTx)
        if fault == .afterReceive { throw LibrarySnapshotError.rejectedIngest }

        let manifestURL = try LibrarySnapshot.path(PushStaging.manifestName, in: privateTx)
        let readyURL = try LibrarySnapshot.path(PushStaging.readyName, in: privateTx)
        guard fm.isReadableFile(atPath: readyURL.path) else { throw LibrarySnapshotError.incompleteRevision }
        let manifestBytes = try Data(contentsOf: manifestURL)
        let ready = try Data(contentsOf: readyURL)
        if !ready.isEmpty && ready != Data(LibrarySnapshot.digest(manifestBytes).utf8) {
            throw LibrarySnapshotError.rejectedIngest
        }
        let manifest = try JSONSerialization.jsonObject(with: manifestBytes) as? [String: Any]
        guard let manifest, manifest["tx_id"] as? String == txId,
              let packages = manifest["packages"] as? [[String: Any]] else {
            throw LibrarySnapshotError.invalidCatalog
        }
        let digest = try PushStaging.requestDigest(txId: txId, packages: packages)
        let declared = manifest["request_digest"] as? String
        if (ready.isEmpty && declared == nil) || (declared != nil && declared != digest) {
            throw LibrarySnapshotError.rejectedIngest
        }
        try verifyPackages(packages, in: privateTx)
        if fault == .afterVerify { throw LibrarySnapshotError.rejectedIngest }

        if let existing = try currentTransaction(txId) {
            if existing.requestDigest != digest { throw LibrarySnapshotError.transactionConflict }
            return try persistReceipt(txId: txId, record: existing, rebuilt: true, fault: fault)
        }

        let libraryRoot = try ensureDocumentsLibrary()
        let listing = try LibrarySnapshot.path("library.json", in: libraryRoot)
        let before: Data?
        do {
            before = try Data(contentsOf: listing)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            before = nil
        }
        let current = try before.map(LibrarySnapshot.init)
        let currentRows = Dictionary(uniqueKeysWithValues: (current?.records ?? []).compactMap { record in
            record.catalogID.map { ($0, record) }
        })
        var installed: [String: (revision: String, previous: String, files: [String: String], row: [String: Any])] = [:]
        for (index, package) in packages.enumerated() {
            guard let id = package["id"] as? String, EpisodeID.isSafe(id),
                  let revision = package["revision"] as? String, EpisodeID.isRevisionDigest(revision),
                  let expected = package["previous_path"] as? String,
                  let files = package["files"] as? [String: [String: Any]] else {
                throw LibrarySnapshotError.invalidCatalog
            }
            let priorRevision = currentRows[id]?.mediaRevision
            let previousPath = priorRevision?.path ?? (currentRows[id] == nil ? "" : "episodes/\(id)")
            guard expected == previousPath else { throw LibrarySnapshotError.staleCatalog }
            let previous = previousPath == "objects/\(revision)" ? (priorRevision?.previousPath ?? "") : previousPath
            if fault == .midInstall && index > 0 { throw LibrarySnapshotError.rejectedIngest }
            let hashes = try installObject(id: id, revision: revision, files: files, from: privateTx, into: libraryRoot, fault: fault)
            var row = package["row"] as? [String: Any] ?? [:]
            row["id"] = id
            installed[id] = (revision, previous, hashes, row)
        }
        if fault == .midInstall && packages.count <= 1 { throw LibrarySnapshotError.rejectedIngest }
        if fault == .beforeCommit { throw LibrarySnapshotError.rejectedIngest }
        if fault == .diskFull { throw LibrarySnapshotError.rejectedIngest }

        let proposedRows: [[String: Any]] = {
            var rows: [String: [String: Any]] = [:]
            if let current {
                for (record, raw) in zip(current.records, current.rows) {
                    if let id = record.catalogID { rows[id] = raw }
                }
            }
            for (id, item) in installed {
                var row = item.row
                if let prior = rows[id] {
                    row = prior.filter { !Self.publisherFields.contains($0.key) }.merging(item.row) { _, incoming in incoming }
                }
                row["id"] = id
                row["media_revision"] = [
                    "sha256": item.revision,
                    "path": "objects/\(item.revision)",
                    "previous_path": item.previous,
                    "resume_policy": "reset_unmapped",
                    "files": item.files,
                ]
                rows[id] = row
            }
            // Keep prior order, then append new ids.
            var ordered: [[String: Any]] = []
            var seen: Set<String> = []
            if let current {
                for record in current.records {
                    if let id = record.catalogID, let row = rows[id] {
                        ordered.append(row)
                        seen.insert(id)
                    }
                }
            }
            for id in installed.keys.sorted() where !seen.contains(id) {
                if let row = rows[id] { ordered.append(row) }
            }
            return ordered
        }()
        var object: [String: Any] = current?.object ?? [:]
        object["episodes"] = proposedRows
        let incoming = try LibrarySnapshot(try LibrarySnapshot.json(object))
        let merged = try incoming.preservingExtensions(from: current)
        let snapshot = try LibrarySnapshot(merged)
        var book = snapshot.transactions
        let generation = (current?.generation ?? 0) + 1
        let parent = before.map(LibrarySnapshot.digest)
        let body = try snapshot.stamped(generation: generation, parent: parent, transactions: [:])
        let result = try LibrarySnapshot(body).bodyDigest()
        book[txId] = CatalogTransaction(requestDigest: digest, generation: generation, resultDigest: result)
        let stamped = try snapshot.stamped(generation: generation, parent: parent, transactions: book)
        let committedDigest = try Self.commitSnapshot(stamped, expecting: before.map(LibrarySnapshot.digest), in: libraryRoot)
        reload()
        let _ = committedDigest
        let record = CatalogTransaction(requestDigest: digest, generation: generation, resultDigest: result)
        return try persistReceipt(txId: txId, record: record, rebuilt: false, fault: fault)
    }

    private static let publisherFields: Set<String> = [
        "id", "episode_id", "source_job_id", "title", "created_at", "duration_sec", "file",
        "youtube_id", "artwork", "artwork_kind", "media_revision", "resume_seconds",
        "resume_sec", "position_seconds", "playback_progress", "language", "audio_kind",
        "transcript_status", "status", "transcript", "manifest", "pending_units", "row",
        "revision", "files", "previous_path",
    ]

    private func verifyPackages(_ packages: [[String: Any]], in root: URL) throws {
        for package in packages {
            guard let revision = package["revision"] as? String,
                  let files = package["files"] as? [String: [String: Any]] else {
                throw LibrarySnapshotError.invalidCatalog
            }
            let folder = try LibrarySnapshot.path("objects/\(revision)", in: root, directory: true)
            for (name, meta) in files {
                guard let sha = meta["sha256"] as? String, let size = meta["size"] as? Int else {
                    throw LibrarySnapshotError.invalidCatalog
                }
                let file = try LibrarySnapshot.path(name, in: folder)
                guard FileManager.default.fileExists(atPath: file.path) else {
                    throw LibrarySnapshotError.incompleteRevision
                }
                let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
                guard attributes[.type] as? FileAttributeType == .typeRegular,
                      (attributes[.size] as? NSNumber)?.intValue == size,
                      (attributes[.referenceCount] as? NSNumber)?.intValue == 1 else {
                    throw LibrarySnapshotError.rejectedIngest
                }
                let bytes = try Data(contentsOf: file)
                guard bytes.count == size, LibrarySnapshot.digest(bytes) == sha else {
                    throw LibrarySnapshotError.incompleteRevision
                }
            }
        }
    }

    private func installObject(id: String, revision: String, files: [String: [String: Any]],
                               from staging: URL, into libraryRoot: URL, fault: IngestFault) throws -> [String: String] {
        let fm = FileManager.default
        let source = try LibrarySnapshot.path("objects/\(revision)", in: staging, directory: true)
        let dest = try LibrarySnapshot.path("objects/\(revision)", in: libraryRoot, directory: true)
        var hashes: [String: String] = [:]
        if fm.fileExists(atPath: dest.path) {
            for (name, meta) in files {
                guard let sha = meta["sha256"] as? String, let size = meta["size"] as? Int else {
                    throw LibrarySnapshotError.invalidCatalog
                }
                let file = try LibrarySnapshot.path(name, in: dest)
                let attributes = try fm.attributesOfItem(atPath: file.path)
                let bytes = try Data(contentsOf: file)
                guard (attributes[.size] as? NSNumber)?.intValue == size,
                      bytes.count == size, LibrarySnapshot.digest(bytes) == sha else {
                    throw LibrarySnapshotError.incompleteRevision
                }
                hashes[name] = sha
            }
            return hashes
        }
        let partial = try LibrarySnapshot.path("objects/\(revision).partial", in: libraryRoot, directory: true)
        if fm.fileExists(atPath: partial.path) { try fm.removeItem(at: partial) }
        try fm.createDirectory(at: partial, withIntermediateDirectories: true)
        var count = 0
        for (name, meta) in files {
            guard let sha = meta["sha256"] as? String, let size = meta["size"] as? Int else {
                throw LibrarySnapshotError.invalidCatalog
            }
            if fault == .midInstall, count == 1 { throw LibrarySnapshotError.rejectedIngest }
            let from = try LibrarySnapshot.path(name, in: source)
            let to = try LibrarySnapshot.path(name, in: partial)
            try fm.copyItem(at: from, to: to)
            let sourceAttrs = try fm.attributesOfItem(atPath: from.path)
            let destAttrs = try fm.attributesOfItem(atPath: to.path)
            if sourceAttrs[.systemFileNumber] as? NSNumber == destAttrs[.systemFileNumber] as? NSNumber {
                throw LibrarySnapshotError.rejectedIngest
            }
            let bytes = try Data(contentsOf: to)
            guard bytes.count == size, LibrarySnapshot.digest(bytes) == sha else {
                throw LibrarySnapshotError.incompleteRevision
            }
            hashes[name] = sha
            count += 1
        }
        try fm.moveItem(at: partial, to: dest)
        return hashes
    }

    private func rejectLinkedPayloads(in root: URL) throws {
        let fm = FileManager.default
        let sourceRoot = root.standardizedFileURL.resolvingSymlinksInPath()
        guard let enumerator = fm.enumerator(at: sourceRoot, includingPropertiesForKeys: [.isSymbolicLinkKey, .isRegularFileKey, .isDirectoryKey], options: []) else {
            throw LibrarySnapshotError.invalidPath
        }
        for case let file as URL in enumerator {
            if (try? file.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                throw LibrarySnapshotError.invalidPath
            }
            guard (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true else { continue }
            let attributes = try fm.attributesOfItem(atPath: file.path)
            if (attributes[.referenceCount] as? NSNumber)?.intValue != 1 {
                throw LibrarySnapshotError.rejectedIngest
            }
        }
    }

    private func copyTree(from source: URL, to dest: URL) throws {

        let fm = FileManager.default
        let sourceRoot = source.standardizedFileURL.resolvingSymlinksInPath()
        _ = try LibrarySnapshot.path(sourceRoot.lastPathComponent, in: sourceRoot.deletingLastPathComponent(), directory: true)
        try fm.createDirectory(at: dest, withIntermediateDirectories: true)
        guard let enumerator = fm.enumerator(at: sourceRoot, includingPropertiesForKeys: [.isSymbolicLinkKey, .isRegularFileKey, .isDirectoryKey], options: []) else {
            throw LibrarySnapshotError.invalidPath
        }
        for case let file as URL in enumerator {
            if (try? file.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                throw LibrarySnapshotError.invalidPath
            }
            let relative: String
            let prefix = sourceRoot.path + "/"
            let filePath = file.standardizedFileURL.path
            if filePath.hasPrefix(prefix) {
                relative = String(filePath.dropFirst(prefix.count))
            } else if file.path.hasPrefix(prefix) {
                relative = String(file.path.dropFirst(prefix.count))
            } else {
                throw LibrarySnapshotError.invalidPath
            }
            if relative.isEmpty { continue }
            let target = try LibrarySnapshot.path(relative, in: dest, directory: (try? file.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true)
            if (try? file.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
                try fm.createDirectory(at: target, withIntermediateDirectories: true)
            } else {
                try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
                try fm.copyItem(at: file, to: target)
                let a = try fm.attributesOfItem(atPath: file.path)
                let b = try fm.attributesOfItem(atPath: target.path)
                if a[.systemFileNumber] as? NSNumber == b[.systemFileNumber] as? NSNumber {
                    throw LibrarySnapshotError.rejectedIngest
                }
            }
        }
    }

    private func ensureDocumentsLibrary() throws -> URL {
        guard let documentsDirectory else { throw LibrarySnapshotError.invalidPath }
        let root = documentsDirectory.appendingPathComponent("中文播客私人库", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    private func currentTransaction(_ txId: String) throws -> CatalogTransaction? {
        guard let root = documentsLibraryURL() else { return nil }
        guard let data = try? Data(contentsOf: try LibrarySnapshot.path("library.json", in: root)) else { return nil }
        return try LibrarySnapshot(data).transactions[txId]
    }

    private func receiptFromLedger(txId: String) throws -> IngestReceipt {
        guard let record = try currentTransaction(txId) else { throw LibrarySnapshotError.transactionConflict }
        return try persistReceipt(txId: txId, record: record, rebuilt: true, fault: .none)
    }

    private func persistReceipt(txId: String, record: CatalogTransaction, rebuilt: Bool, fault: IngestFault) throws -> IngestReceipt {
        let receipt = IngestReceipt(txId: txId, requestDigest: record.requestDigest,
                                    generation: record.generation, resultDigest: record.resultDigest, rebuilt: rebuilt)
        if fault == .afterCommitBeforeReceipt { throw LibrarySnapshotError.rejectedIngest }
        guard let documentsDirectory else { return receipt }
        let url = documentsDirectory.appendingPathComponent(PushStaging.stagingName, isDirectory: true)
            .appendingPathComponent(txId, isDirectory: true)
            .appendingPathComponent(PushStaging.receiptName)
        if FileManager.default.fileExists(atPath: url.deletingLastPathComponent().path) {
            let body: [String: Any] = [
                "tx_id": receipt.txId,
                "request_digest": receipt.requestDigest,
                "generation": receipt.generation,
                "result_digest": receipt.resultDigest,
                "state": "RECEIPTED",
                "rebuilt": receipt.rebuilt,
            ]
            try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]).write(to: url, options: .atomic)
        }
        return receipt
    }

    private func migrateLegacyLibraryLocked(fault: IngestFault) throws -> IngestReceipt? {
        let libraryRoot = try ensureDocumentsLibrary()
        let listing = try LibrarySnapshot.path("library.json", in: libraryRoot)
        let before: Data
        do {
            before = try Data(contentsOf: listing)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return nil
        }
        let current = try LibrarySnapshot(before)
        func needsMigration(_ record: EpisodeRecord) -> Bool {
            // Pending transcripts are bound to episodes/<id> and validated
            // there by commitSnapshot. The revision format excludes them.
            if record.declaresPendingTranscript { return false }
            guard let path = record.mediaRevision?.path else { return true }
            return !path.hasPrefix("objects/") && !path.hasPrefix("revisions/")
        }
        if current.generation > 0 && !current.records.contains(where: needsMigration) { return nil }
        let backup = libraryRoot.appendingPathComponent(".migration-backup", isDirectory: true)
        try FileManager.default.createDirectory(at: backup, withIntermediateDirectories: true)
        try before.write(to: backup.appendingPathComponent("library.json"), options: .atomic)
        var rows: [[String: Any]] = []
        for (record, raw) in zip(current.records, current.rows) {
            guard let id = record.catalogID, EpisodeID.isSafe(id) else { throw LibrarySnapshotError.invalidCatalog }
            if !needsMigration(record) {
                rows.append(raw)
                continue
            }
            let previous = "episodes/\(id)"
            let folder = try LibrarySnapshot.path(previous, in: libraryRoot, directory: true)
            var files: [String: Data] = [:]
            for name in ["output.mp3", "document.json", "source-identity.json", "render-identity.json", "episode.json"] {
                let url = try LibrarySnapshot.path(name, in: folder)
                if FileManager.default.isReadableFile(atPath: url.path) {
                    let data = try Data(contentsOf: url)
                    files[name] = data
                }
            }
            guard files["output.mp3"] != nil else { throw LibrarySnapshotError.incompleteRevision }
            if let artwork = EpisodeArtwork.file(record.artwork, kind: record.artworkKind) {
                let url = try LibrarySnapshot.path(artwork, in: folder)
                guard LocalAudio.isPlayable(url) else { throw LibrarySnapshotError.incompleteRevision }
                files[artwork] = try Data(contentsOf: url)
            }
            if files["document.json"] == nil { files["document.json"] = Data("{}".utf8) }
            if files["source-identity.json"] == nil { files["source-identity.json"] = Data("{}".utf8) }
            if files["render-identity.json"] == nil { files["render-identity.json"] = Data("{}".utf8) }
            if files["episode.json"] == nil { files["episode.json"] = Data("{}".utf8) }
            let hashes = files.mapValues(LibrarySnapshot.digest)
            let fileLines = hashes.sorted(by: { $0.key < $1.key }).map { "  \"\($0.key)\": \"\($0.value)\"" }
            let revision = LibrarySnapshot.digest(Data(("{\n" + fileLines.joined(separator: ",\n") + "\n}\n").utf8))
            let destPartial = try LibrarySnapshot.path("objects/\(revision).partial", in: libraryRoot, directory: true)
            if FileManager.default.fileExists(atPath: destPartial.path) {
                try FileManager.default.removeItem(at: destPartial)
            }
            try FileManager.default.createDirectory(at: destPartial, withIntermediateDirectories: true)
            var count = 0
            for (name, data) in files {
                if fault == .midInstall, count == 1 { throw LibrarySnapshotError.rejectedIngest }
                try data.write(to: destPartial.appendingPathComponent(name), options: .atomic)
                count += 1
            }
            if fault == .midInstall { throw LibrarySnapshotError.rejectedIngest }
            let dest = try LibrarySnapshot.path("objects/\(revision)", in: libraryRoot, directory: true)
            if FileManager.default.fileExists(atPath: dest.path) {
                try FileManager.default.removeItem(at: destPartial)
            } else {
                try FileManager.default.moveItem(at: destPartial, to: dest)
            }
            var row = raw
            row["media_revision"] = [
                "sha256": revision,
                "path": "objects/\(revision)",
                "previous_path": previous,
                "resume_policy": "reset_unmapped",
                "files": hashes,
            ]
            rows.append(row)
        }
        if fault == .beforeCommit { throw LibrarySnapshotError.rejectedIngest }
        var object = current.object
        object["episodes"] = rows
        let incoming = try LibrarySnapshot(try LibrarySnapshot.json(object))
        let merged = try incoming.preservingExtensions(from: current)
        let snapshot = try LibrarySnapshot(merged)
        let txId = "migrate-legacy"
        let digest = LibrarySnapshot.digest(before)
        var book = snapshot.transactions
        let generation = current.generation + 1
        let parent = LibrarySnapshot.digest(before)
        let body = try snapshot.stamped(generation: generation, parent: parent, transactions: [:])
        let result = try LibrarySnapshot(body).bodyDigest()
        book[txId] = CatalogTransaction(requestDigest: digest, generation: generation, resultDigest: result)
        let stamped = try snapshot.stamped(generation: generation, parent: parent, transactions: book)
        if fault == .diskFull { throw LibrarySnapshotError.rejectedIngest }
        let committed = try Self.commitSnapshot(stamped, expecting: LibrarySnapshot.digest(before), in: libraryRoot)
        reload()
        // Same-bytes migration is not a content revision. Copy the legacy id
        // mark onto the new media key so progress stays visible, and keep the
        // original id key as the source record.
        for episode in episodes {
            if progress[episode.mediaKey] == nil, let mark = progress[episode.id] {
                progress[episode.mediaKey] = mark
            }
        }
        saveProgressMap()
        let _ = committed
        let record = CatalogTransaction(requestDigest: digest, generation: generation, resultDigest: result)
        return try persistReceipt(txId: txId, record: record, rebuilt: false, fault: fault)
    }
}
