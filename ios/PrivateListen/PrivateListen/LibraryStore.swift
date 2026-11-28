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
    private(set) var hasLoadedOnce = false

    private struct RevisionSpec: Sendable {
        let root: URL
        let folder: URL
        let localCopy: Bool
        let revision: MediaRevision
        let declaredArtwork: String?
    }

    private struct VerificationSignature: Equatable {
        let root: URL
        let fingerprintsByMediaKey: [String: [String: RevisionAssetVerifier.FileFingerprint?]]

        func covers(_ other: VerificationSignature) -> Bool {
            guard root == other.root else { return false }
            return other.fingerprintsByMediaKey.allSatisfy { key, fingerprints in
                fingerprintsByMediaKey[key] == fingerprints
            }
        }
    }

    private struct VerificationOutcome: Sendable {
        let episodeID: String
        let mediaKey: String
        let root: URL
        let unavailableReason: Episode.UnavailableReason?
        let revisionFiles: Set<String>
        let artworkFile: String?
    }

    private final class VerificationFlight: @unchecked Sendable {
        let id = UUID()
        let task: Task<Episode?, Never>
        private let lock = NSLock()
        private var activeTokens: Set<UUID> = []

        init(task: Task<Episode?, Never>) {
            self.task = task
        }

        func acquireToken() -> UUID {
            let token = UUID()
            lock.lock()
            activeTokens.insert(token)
            lock.unlock()
            return token
        }

        func releaseToken(_ token: UUID, cancelIfEmpty: Bool) {
            lock.lock()
            let removed = activeTokens.remove(token) != nil
            let shouldCancel = removed && cancelIfEmpty && activeTokens.isEmpty
            lock.unlock()
            if shouldCancel {
                task.cancel()
            }
        }
    }

    private var revisionSpecsByKey: [String: RevisionSpec] = [:]
    private var verificationGeneration: UInt64 = 0
    private var verificationTask: Task<Void, Never>?
    private var activeVerificationSignature: VerificationSignature?
    private var inFlightEpisodeVerifications: [String: VerificationFlight] = [:]

    #if DEBUG || os(macOS)
    var verifierForTesting: RevisionAssetVerifier { revisionVerifier }
    #endif

    private let defaults: UserDefaults
    private let documentsDirectory: URL?
    private let allowHostCloudDiscovery: Bool
    private let ossConfig: OSSConfig
    private let ossPuller: OSSEpisodePuller
    private var ossPullTask: Task<Void, Never>?
    /// In-process ingest lock. Must not live in UserDefaults: a crash would stick it.
    private var ingestBusy = false

    init(defaults: UserDefaults = .standard, loadLibraryOnInit: Bool = true,
         documentsDirectory: URL? = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first,
         ossConfig: OSSConfig? = nil,
         ossPuller: OSSEpisodePuller? = nil) {
        self.defaults = defaults
        self.documentsDirectory = documentsDirectory
        self.ossConfig = ossConfig ?? OSSConfig(defaults: defaults)
        self.ossPuller = ossPuller ?? OSSEpisodePuller()
        let defaultDocs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        self.allowHostCloudDiscovery = documentsDirectory?.standardizedFileURL == defaultDocs?.standardizedFileURL
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

    deinit {
        ossPullTask?.cancel()
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

    /// Prefer the current media key. Fall back to the bare catalog id only when
    /// no revision-qualified key exists yet (same-bytes gen2 promotion). A later
    /// unmapped audio revision must not inherit that bare-id cursor.
    nonisolated static func hasRevisionQualifiedKey(in progress: [String: ListenMark], episodeID: String) -> Bool {
        let prefix = episodeID + "."
        return progress.keys.contains { $0.hasPrefix(prefix) }
    }

    nonisolated static func resolvedListenMark(
        in progress: [String: ListenMark],
        mediaKey: String,
        episodeID: String
    ) -> ListenMark? {
        if let mark = progress[mediaKey] { return mark }
        if mediaKey != episodeID, Self.hasRevisionQualifiedKey(in: progress, episodeID: episodeID) {
            return nil
        }
        return progress[episodeID]
    }

    nonisolated static func resolvedTouch(
        in touched: [String: TimeInterval],
        progress: [String: ListenMark],
        mediaKey: String,
        episodeID: String
    ) -> TimeInterval {
        if let value = touched[mediaKey] { return value }
        if mediaKey != episodeID, Self.hasRevisionQualifiedKey(in: progress, episodeID: episodeID) {
            return 0
        }
        return touched[episodeID] ?? 0
    }

    private func listenMark(for id: String) -> ListenMark? {
        let key = mediaKey(for: id)
        return Self.resolvedListenMark(in: progress, mediaKey: key, episodeID: id)
    }

    private func listenMark(for episode: Episode) -> ListenMark? {
        Self.resolvedListenMark(in: progress, mediaKey: episode.mediaKey, episodeID: episode.id)
    }

    /// Copy a bare-id mark onto the current media key when that key is still empty
    /// and no other revision key exists. Never overwrites a newer key; leaves the
    /// bare-id record in place.
    private func adoptLegacyListenMarks(in episodes: [Episode]) {
        var changed = false
        var touchesChanged = false
        for episode in episodes where episode.unavailableReason == nil {
            let key = episode.mediaKey
            guard key != episode.id, progress[key] == nil else { continue }
            guard !Self.hasRevisionQualifiedKey(in: progress, episodeID: episode.id) else { continue }
            guard let mark = progress[episode.id] else { continue }
            progress[key] = mark
            changed = true
            if continueTouched[key] == nil, let touched = continueTouched[episode.id] {
                continueTouched[key] = touched
                touchesChanged = true
            }
        }
        if changed { saveProgressMap() }
        if touchesChanged { saveContinueBookkeeping() }
    }

    private func isContinueDismissed(_ episode: Episode) -> Bool {
        continueDismissedIDs.contains { key in
            key == episode.id || key == episode.mediaKey || key.hasPrefix(episode.id + ".")
        }
    }

    private func episodeID(fromProgressKey key: String) -> String {
        key.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? key
    }

    private func dismissKeys(for id: String) -> Set<String> {
        let current = mediaKey(for: id)
        var keys: Set<String> = [id, current]
        for key in continueDismissedIDs where key == id || key.hasPrefix(id + ".") {
            keys.insert(key)
        }
        for key in progress.keys where key == id || key.hasPrefix(id + ".") {
            keys.insert(key)
        }
        for key in continueTouched.keys where key == id || key.hasPrefix(id + ".") {
            keys.insert(key)
        }
        return keys
    }

    func isUnplayed(_ id: String) -> Bool {
        Self.isUnplayed(listenMark(for: id))
    }

    func isFinished(_ id: String) -> Bool {
        listenMark(for: id)?.finished == true
    }

    /// In progress: started (≥5s) and not finished.
    func isInProgress(_ id: String) -> Bool {
        guard let entry = listenMark(for: id), !entry.finished else { return false }
        return entry.seconds >= 5
    }

    /// Label for an unfinished shelf row. Continue displays replay cursors separately.
    func continuePointLabel(for id: String) -> String? {
        guard let entry = listenMark(for: id), !entry.finished, entry.seconds >= 5 else { return nil }
        return "上次听到 \(listenClock(entry.seconds))"
    }

    func playedFraction(for episode: Episode) -> Double? {
        guard let entry = listenMark(for: episode), !entry.finished, entry.seconds >= 5 else { return nil }
        let duration = episode.durationSec ?? 0
        guard duration > 1 else { return nil }
        return min(1, max(0, entry.seconds / duration))
    }

    func resumeSeconds(for id: String) -> Double {
        Self.resumeSeconds(from: listenMark(for: id))
    }

    func resumeSeconds(for episode: Episode) -> Double {
        Self.resumeSeconds(from: listenMark(for: episode))
    }

    func isFinished(_ episode: Episode) -> Bool {
        listenMark(for: episode)?.finished == true
    }

    /// Returns whether this sample changed the episode from unfinished to finished.
    @discardableResult
    func remember(id: String, seconds: Double, duration: Double, finished: Bool? = nil) -> Bool {
        guard visibleByID[id]?.unavailableReason == nil else { return false }
        return remember(mediaKey: mediaKey(for: id), seconds: seconds, duration: duration, finished: finished)
    }

    @discardableResult
    func remember(episode: Episode, seconds: Double, duration: Double, finished: Bool? = nil) -> Bool {
        guard episode.unavailableReason == nil, visibleByID[episode.id]?.unavailableReason == nil else { return false }
        return remember(mediaKey: episode.mediaKey, seconds: seconds, duration: duration, finished: finished)
    }

    private func remember(mediaKey: String, seconds: Double, duration: Double, finished: Bool?) -> Bool {
        guard seconds.isFinite, seconds >= 0, !mediaKey.isEmpty else { return false }
        let episodeID = episodeID(fromProgressKey: mediaKey)
        let current = Self.resolvedListenMark(in: progress, mediaKey: mediaKey, episodeID: episodeID)
        let wasFinished = current?.finished == true
        var map = progress
        if map[mediaKey] == nil, let inherited = current {
            map[mediaKey] = inherited
        }
        if let next = Self.reducing(map, id: mediaKey, seconds: seconds, duration: duration, finished: finished) {
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
        let key = mediaKey(for: id)
        progress[key] = ListenMark(
            seconds: listenMark(for: id)?.seconds ?? 0,
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
        let key = mediaKey(for: id)
        let previous = listenMark(for: id)
        let seconds: Double
        if let cursor = previous?.replaySeconds, cursor.isFinite, cursor > 0 {
            seconds = cursor
        } else {
            seconds = previous?.seconds ?? 0
        }
        progress[key] = ListenMark(
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
        for episode in episodes where episode.unavailableReason == nil {
            guard let entry = listenMark(for: episode) else { continue }
            marks[episode.id] = ContinueListening.Mark(
                seconds: entry.seconds,
                finished: entry.finished,
                touchedAt: Self.resolvedTouch(
                    in: continueTouched,
                    progress: progress,
                    mediaKey: episode.mediaKey,
                    episodeID: episode.id
                ),
                replaySeconds: entry.replaySeconds
            )
            durations[episode.id] = episode.durationSec ?? 0
        }
        return ContinueListening.items(
            marks: marks,
            durations: durations,
            dismissed: Set(episodes.filter { isContinueDismissed($0) }.map(\.id))
        )
    }

    /// Left swipe on a continue row. Does not delete the episode or its ListenMark.
    /// Records the bare id, the current media key, and any already stored revision
    /// aliases so a later bare-id fallback cannot resurrect the card.
    func dismissContinue(_ id: String) {
        guard !id.isEmpty else { return }
        var next = continueDismissedIDs
        for key in dismissKeys(for: id) {
            next = ContinueListening.dismissing(next, id: key)
        }
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
        let episodeID = episodeID(fromProgressKey: mediaKey)
        let next = continueDismissedIDs.filter { key in
            key != episodeID && key != mediaKey && !key.hasPrefix(episodeID + ".")
        }
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
        resumeSeconds(from: progress[id])
    }

    nonisolated static func resumeSeconds(from entry: ListenMark?) -> Double {
        guard let entry else { return 0 }
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
            cancelBackgroundVerification()
            revisionSpecsByKey = [:]
            lastSnapshot = nil
            visibleByID = [:]
            episodes = []
            waitingOnCloud = false
            hasLoadedOnce = true
            return
        }

        let loaded = Self.loadEpisodesWithSpecs(
            from: root,
            localCopy: folderURL == nil,
            verifier: revisionVerifier,
            mode: .lightweight
        )
        if loaded.result.accepted {
            lastSnapshot = (root, loaded.result.episodes)
            revisionSpecsByKey = loaded.specsByKey
            pruneInFlightVerifications(keepingKeys: Set(loaded.specsByKey.keys))
        } else if lastSnapshot?.root != root {
            revisionSpecsByKey = [:]
            pruneInFlightVerifications(keepingKeys: [])
        }
        let visible = lastSnapshot?.root == root ? lastSnapshot?.episodes ?? [] : []
        visibleByID = Dictionary(uniqueKeysWithValues: visible.map { ($0.id, $0) })
        episodes = visible
        waitingOnCloud = loaded.result.waiting || visible.contains { $0.downloading }
        adoptLegacyListenMarks(in: visible)
        hasLoadedOnce = true
        if lastSnapshot?.root == root {
            scheduleBackgroundVerification(root: root, episodes: visible)
        } else {
            cancelBackgroundVerification()
        }
    }

    func waitForVerification() async {
        while let task = verificationTask {
            await task.value
        }
    }

    func isEpisodeVerified(_ episode: Episode) -> Bool {
        if episode.unavailableReason != nil || episode.mediaRevision == nil {
            return true
        }
        guard let spec = revisionSpecsByKey[episode.mediaKey] else {
            return true
        }
        for (name, digest) in spec.revision.files {
            guard let asset = try? LibrarySnapshot.path(name, in: spec.folder) else {
                return false
            }
            switch revisionVerifier.lightweightStatus(asset, digest: digest) {
            case .verified:
                continue
            case .unverifiedCandidate:
                return false
            case .failed:
                if MediaRevision.requiredFiles.contains(name)
                    || episode.revisionFiles?.contains(name) != false {
                    return false
                }
            }
        }
        return true
    }

    func verifyEpisodeNow(_ episode: Episode) async -> Episode {
        let current = visibleByID[episode.id] ?? episode
        guard current.mediaRevision != nil, current.unavailableReason == nil else {
            return current
        }
        if isEpisodeVerified(current) {
            return current
        }
        let key = current.mediaKey
        guard let spec = revisionSpecsByKey[key] else {
            return current
        }
        if Task.isCancelled {
            return current
        }
        let flight: VerificationFlight
        if let existing = inFlightEpisodeVerifications[key] {
            flight = existing
        } else {
            let generation = verificationGeneration
            let verifier = revisionVerifier
            let episodeID = current.id
            let driver = Task<Episode?, Never> { [weak self] in
                let outcome = await Self.runSingleVerification(
                    episodeID: episodeID,
                    mediaKey: key,
                    spec: spec,
                    verifier: verifier
                )
                guard let self else { return nil }
                guard let outcome else { return nil }
                return self.applyVerificationOutcome(outcome, expectedGeneration: generation)
            }
            let createdFlight = VerificationFlight(task: driver)
            inFlightEpisodeVerifications[key] = createdFlight
            flight = createdFlight
            Task { @MainActor [weak self] in
                _ = await createdFlight.task.value
                guard let self else { return }
                if self.inFlightEpisodeVerifications[key]?.id == createdFlight.id {
                    self.inFlightEpisodeVerifications.removeValue(forKey: key)
                }
            }
        }
        let token = flight.acquireToken()
        let result = await withTaskCancellationHandler {
            await flight.task.value
        } onCancel: {
            flight.releaseToken(token, cancelIfEmpty: true)
        }
        flight.releaseToken(token, cancelIfEmpty: false)
        return result ?? visibleByID[current.id] ?? current
    }

    private func pruneInFlightVerifications(keepingKeys: Set<String>) {
        for (key, flight) in inFlightEpisodeVerifications where !keepingKeys.contains(key) {
            flight.task.cancel()
            inFlightEpisodeVerifications.removeValue(forKey: key)
        }
    }

    private func cancelBackgroundVerification() {
        verificationTask?.cancel()
        verificationTask = nil
        activeVerificationSignature = nil
        verificationGeneration &+= 1
    }

    private func scheduleBackgroundVerification(root: URL, episodes: [Episode]) {
        let pending = episodes.filter { episode in
            episode.unavailableReason == nil
                && episode.mediaRevision != nil
                && revisionSpecsByKey[episode.mediaKey] != nil
                && !isEpisodeVerified(episode)
        }
        guard !pending.isEmpty else {
            cancelBackgroundVerification()
            return
        }
        let continueIDs = Set(continueEntries(in: episodes).map(\.id))
        let orderedPending = pending.filter { continueIDs.contains($0.id) }
            + pending.filter { !continueIDs.contains($0.id) }
        var fingerprintsByMediaKey: [String: [String: RevisionAssetVerifier.FileFingerprint?]] = [:]
        for ep in orderedPending {
            guard let spec = revisionSpecsByKey[ep.mediaKey] else { continue }
            var fileMap: [String: RevisionAssetVerifier.FileFingerprint?] = [:]
            for name in spec.revision.files.keys {
                let asset = try? LibrarySnapshot.path(name, in: spec.folder)
                fileMap[name] = asset.flatMap { revisionVerifier.fingerprint(of: $0) }
            }
            fingerprintsByMediaKey[ep.mediaKey] = fileMap
        }
        let signature = VerificationSignature(root: root, fingerprintsByMediaKey: fingerprintsByMediaKey)
        if verificationTask != nil,
           let active = activeVerificationSignature,
           active.covers(signature) {
            return
        }

        verificationTask?.cancel()
        verificationGeneration &+= 1
        let generation = verificationGeneration
        activeVerificationSignature = signature
        let workItems: [(id: String, mediaKey: String, spec: RevisionSpec)] = orderedPending.compactMap { ep in
            guard let spec = revisionSpecsByKey[ep.mediaKey] else { return nil }
            return (ep.id, ep.mediaKey, spec)
        }
        let verifier = revisionVerifier
        verificationTask = Task.detached(priority: .utility) { [weak self] in
            for item in workItems {
                if Task.isCancelled { break }
                do {
                    let outcome = try Self.performVerification(
                        episodeID: item.id,
                        mediaKey: item.mediaKey,
                        spec: item.spec,
                        verifier: verifier,
                        checkCancellation: true
                    )
                    if Task.isCancelled { break }
                    await MainActor.run { [weak self] in
                        guard let self else { return }
                        _ = self.applyVerificationOutcome(outcome, expectedGeneration: generation)
                    }
                } catch is CancellationError {
                    break
                } catch {
                    continue
                }
            }
            await MainActor.run { [weak self] in
                guard let self, self.verificationGeneration == generation else { return }
                self.verificationTask = nil
                self.activeVerificationSignature = nil
            }
        }
    }

    @discardableResult
    private func applyVerificationOutcome(_ outcome: VerificationOutcome, expectedGeneration: UInt64?) -> Episode? {
        if let expectedGeneration, expectedGeneration != verificationGeneration {
            return nil
        }
        guard lastSnapshot?.root == outcome.root else { return nil }
        guard let index = episodes.firstIndex(where: { $0.id == outcome.episodeID && $0.mediaKey == outcome.mediaKey }) else {
            return nil
        }
        let old = episodes[index]
        let changed = old.unavailableReason != outcome.unavailableReason
            || old.revisionFiles != outcome.revisionFiles
            || old.artworkFile != outcome.artworkFile
        guard changed else { return old }

        var updated = old
        updated.unavailableReason = outcome.unavailableReason
        updated.revisionFiles = outcome.revisionFiles
        updated.artworkFile = outcome.artworkFile

        var nextEpisodes = episodes
        nextEpisodes[index] = updated
        episodes = nextEpisodes
        visibleByID[updated.id] = updated
        if let snapshot = lastSnapshot, snapshot.root == outcome.root {
            var snapshotEpisodes = snapshot.episodes
            if let snapIdx = snapshotEpisodes.firstIndex(where: { $0.id == updated.id && $0.mediaKey == updated.mediaKey }) {
                snapshotEpisodes[snapIdx] = updated
                lastSnapshot = (snapshot.root, snapshotEpisodes)
            }
        }
        let hasCloudWaiting = hasCloudFolder && nextEpisodes.contains { $0.unavailableReason != nil }
        waitingOnCloud = hasCloudWaiting || nextEpisodes.contains { $0.downloading }
        return updated
    }

    nonisolated private static func runSingleVerification(
        episodeID: String,
        mediaKey: String,
        spec: RevisionSpec,
        verifier: RevisionAssetVerifier
    ) async -> VerificationOutcome? {
        let detached = Task.detached(priority: .userInitiated) { () -> VerificationOutcome? in
            do {
                return try performVerification(
                    episodeID: episodeID,
                    mediaKey: mediaKey,
                    spec: spec,
                    verifier: verifier,
                    checkCancellation: true
                )
            } catch is CancellationError {
                return nil
            } catch {
                return nil
            }
        }
        return await withTaskCancellationHandler {
            await detached.value
        } onCancel: {
            detached.cancel()
        }
    }

    nonisolated private static func performVerification(
        episodeID: String,
        mediaKey: String,
        spec: RevisionSpec,
        verifier: RevisionAssetVerifier,
        checkCancellation: Bool
    ) throws -> VerificationOutcome {
        var verified = Set<String>()
        for (name, digest) in spec.revision.files {
            if checkCancellation { try Task.checkCancellation() }
            let asset: URL
            do {
                asset = try LibrarySnapshot.path(name, in: spec.folder)
            } catch {
                continue
            }
            if !spec.localCopy && !LocalAudio.isPlayable(asset) {
                continue
            }
            do {
                try verifier.validate(asset, digest: digest, checkCancellation: checkCancellation)
                verified.insert(name)
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                continue
            }
        }
        let unavailableReason: Episode.UnavailableReason?
        if !verified.contains("output.mp3") {
            unavailableReason = .audio
        } else if !MediaRevision.requiredFiles.isSubset(of: verified) {
            unavailableReason = .revision
        } else {
            unavailableReason = nil
        }
        let artworkFile: String?
        if let declared = spec.declaredArtwork, verified.contains(declared) {
            artworkFile = declared
        } else {
            artworkFile = nil
        }
        return VerificationOutcome(
            episodeID: episodeID,
            mediaKey: mediaKey,
            root: spec.root,
            unavailableReason: unavailableReason,
            revisionFiles: verified,
            artworkFile: artworkFile
        )
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
        let current = try before.map { try LibrarySnapshot($0) }
        let proposed = try incoming.preservingExtensions(from: current)
        let snapshot = try LibrarySnapshot(proposed)
        let repeated = try current?.canonicalData() == proposed
        let actual = before.map(LibrarySnapshot.digest)
        guard repeated || actual == expectedSHA256 else { throw LibrarySnapshotError.staleCatalog }
        let resolved = try Self.resolve(
            snapshot,
            from: root,
            localCopy: true,
            verifier: RevisionAssetVerifier(),
            strict: true,
            mode: .full
        ).episodes
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
        if allowHostCloudDiscovery, let known = knownLibraryURL() {
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

    static func loadEpisodes(from root: URL, localCopy: Bool, verification: AssetVerificationMode = .lightweight) -> LoadedLibrary {
        loadEpisodesWithSpecs(from: root, localCopy: localCopy, verifier: RevisionAssetVerifier(), mode: verification).result
    }

    private static func loadEpisodesWithSpecs(
        from root: URL,
        localCopy: Bool,
        verifier: RevisionAssetVerifier,
        mode: AssetVerificationMode
    ) -> (result: LoadedLibrary, specsByKey: [String: RevisionSpec]) {
        do {
            let listing = try LibrarySnapshot.path("library.json", in: root)
            let fileManager = FileManager.default
            if !localCopy {
                try? fileManager.startDownloadingUbiquitousItem(at: listing)
                guard !CloudFile.isWaiting(listing) else { return (LoadedLibrary(waiting: true), [:]) }
            }
            let bytes = !localCopy && fileManager.isUbiquitousItem(at: listing)
                ? readData(listing) : try? Data(contentsOf: listing)
            guard let data = bytes else { return (LoadedLibrary(waiting: !localCopy), [:]) }
            let snapshot = try LibrarySnapshot(data, allowInvalidRevisions: true)
            let resolved = try resolve(snapshot, from: root, localCopy: localCopy, verifier: verifier, strict: false, mode: mode)
            let waiting = !localCopy && resolved.episodes.contains { $0.unavailableReason != nil }
            return (LoadedLibrary(episodes: resolved.episodes, waiting: waiting, accepted: true), resolved.specsByKey)
        } catch {
            return (LoadedLibrary(waiting: !localCopy), [:])
        }
    }

    private static func resolve(
        _ snapshot: LibrarySnapshot,
        from root: URL,
        localCopy: Bool,
        verifier: RevisionAssetVerifier,
        strict: Bool,
        mode: AssetVerificationMode
    ) throws -> (episodes: [Episode], specsByKey: [String: RevisionSpec]) {
        let effectiveMode: AssetVerificationMode = strict ? .full : mode
        let fileManager = FileManager.default
        var specsByKey: [String: RevisionSpec] = [:]
        let episodes = try snapshot.records.map { record -> Episode in
            guard let id = record.catalogID else { throw LibrarySnapshotError.invalidCatalog }
            let title = record.title?.trimmingCharacters(in: .whitespacesAndNewlines)
            let revision = record.mediaRevision
            var audioURL = root
            var unavailableReason: Episode.UnavailableReason?
            var downloading = false
            var revisionFiles: Set<String>?
            let declaredArtwork = EpisodeArtwork.file(record.artwork, kind: record.artworkKind)
            var artworkFile = declaredArtwork
            var resolvedFolder: URL?
            do {
                guard !snapshot.invalidRevisionIDs.contains(id) else { throw LibrarySnapshotError.invalidCatalog }
                let folder = try LibrarySnapshot.path(revision?.path ?? "episodes/\(id)", in: root, directory: true)
                resolvedFolder = folder
                audioURL = try LibrarySnapshot.path("output.mp3", in: folder)
                if let revision {
                    let verified = try revision.verifiedFiles(
                        in: folder,
                        downloading: !localCopy,
                        verifier: verifier,
                        allowMissing: !strict,
                        mode: effectiveMode
                    )
                    revisionFiles = verified
                    if !verified.contains("output.mp3") {
                        unavailableReason = .audio
                    } else if !MediaRevision.requiredFiles.isSubset(of: verified) {
                        unavailableReason = .revision
                    }
                } else if !LocalAudio.isPlayable(audioURL) {
                    if !localCopy && (CloudFile.isWaiting(audioURL) || CloudFile.isWaiting(folder)) {
                        downloading = true
                        try? fileManager.startDownloadingUbiquitousItem(at: folder)
                        try? fileManager.startDownloadingUbiquitousItem(at: audioURL)
                    } else {
                        guard !strict else { throw LibrarySnapshotError.incompleteRevision }
                        unavailableReason = .audio
                    }
                }
                if let name = artworkFile {
                    do {
                        guard revisionFiles?.contains(name) != false else { throw LibrarySnapshotError.invalidCatalog }
                        let artworkURL = try LibrarySnapshot.path(name, in: folder)
                        if !localCopy && record.artworkKind != "placeholder" && !LocalAudio.isPlayable(artworkURL) {
                            try? fileManager.startDownloadingUbiquitousItem(at: artworkURL)
                        }
                    } catch where !strict {
                        artworkFile = nil
                    }
                }
            } catch where !strict {
                unavailableReason = .revision
                revisionFiles = []
                artworkFile = nil
            }
            let show = record.show?.trimmingCharacters(in: .whitespacesAndNewlines)
            let guests = (record.guests ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            let lensIds = (record.lensIds ?? []).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            var companions: Episode.LegacyCompanions?
            if unavailableReason == nil, let revision, revision.previousPath == "episodes/\(id)",
               let digest = revision.files["output.mp3"],
               let folder = try? LibrarySnapshot.path(revision.previousPath, in: root, directory: true) {
                companions = .init(folder: folder, audioDigest: digest, declaredFiles: Set(revision.files.keys))
            }
            var hasChineseRenderSidecar = false
            if revision == nil, unavailableReason == nil,
               let folder = try? LibrarySnapshot.path("episodes/\(id)", in: root, directory: true) {
                hasChineseRenderSidecar = ["render-identity.json", "document.json"].contains { name in
                    guard let url = try? LibrarySnapshot.path(name, in: folder) else { return false }
                    return LocalAudio.isPlayable(url)
                }
            }
            let episode = Episode(
                id: id,
                title: title.flatMap { $0.isEmpty ? nil : $0 } ?? id,
                show: show.flatMap { $0.isEmpty ? nil : $0 },
                guests: guests,
                lensIds: lensIds,
                createdAt: EpisodeDate.parse(record.createdAt) ?? Date(timeIntervalSince1970: 0),
                durationSec: record.durationSec,
                audioURL: audioURL,
                downloading: downloading,
                artworkFile: artworkFile,
                artworkKind: record.artworkKind,
                pendingTranscript: record.pendingReference(for: id),
                mediaRevision: revision.flatMap { EpisodeID.isRevisionDigest($0.sha256) ? $0.sha256 : nil },
                revisionFiles: revisionFiles,
                unavailableReason: unavailableReason,
                legacyCompanions: companions,
                audioKind: record.audioKind,
                hasChineseRenderSidecar: hasChineseRenderSidecar
            )
            if unavailableReason == nil, let revision, let resolvedFolder, episode.mediaRevision != nil {
                specsByKey[episode.mediaKey] = RevisionSpec(
                    root: root,
                    folder: resolvedFolder,
                    localCopy: localCopy,
                    revision: revision,
                    declaredArtwork: declaredArtwork
                )
            }
            return episode
        }.sorted { lhs, rhs in
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt > rhs.createdAt }
            return lhs.id > rhs.id
        }
        return (episodes, specsByKey)
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
        triggerOSSPullIfConfigured()
    }

    func waitForOSSPull() async {
        while let task = ossPullTask {
            await task.value
        }
    }

    private func triggerOSSPullIfConfigured() {
        guard ossConfig.isConfigured else { return }
        guard folderURL == nil else { return }
        guard documentsDirectory != nil else { return }
        guard ossPullTask == nil else { return }
        let fingerprint = ossConfig.fingerprint
        guard ossPuller.shouldAttemptNow(configFingerprint: fingerprint) else { return }
        guard let baseURL = ossConfig.bridgeBaseURL, let token = ossConfig.bridgeToken else { return }

        var skipIDs = Set<String>()
        var repairableAudioDigests: [String: String] = [:]
        if let root = documentsLibraryURL(),
           let listing = try? LibrarySnapshot.path("library.json", in: root),
           let data = try? Data(contentsOf: listing),
           let snapshot = try? LibrarySnapshot(data, allowInvalidRevisions: true) {
            for record in snapshot.records {
                guard let id = record.catalogID else { continue }
                let folder = try? LibrarySnapshot.path(record.mediaRevision?.path ?? "episodes/\(id)", in: root, directory: true)
                let audioURL = folder.flatMap { try? LibrarySnapshot.path("output.mp3", in: $0) }
                if let audioURL, LocalAudio.isPlayable(audioURL) {
                    skipIDs.insert(id)
                } else if let rev = record.mediaRevision, let audioHash = rev.files["output.mp3"] {
                    repairableAudioDigests[id] = audioHash
                } else {
                    skipIDs.insert(id)
                }
            }
        }

        let puller = ossPuller
        ossPullTask = Task { [weak self] in
            defer { self?.ossPullTask = nil }
            let catalog: [OSSRemoteCatalogItem]
            do {
                catalog = try await puller.fetchCatalog(baseURL: baseURL, token: token)
            } catch is CancellationError {
                return
            } catch {
                puller.recordFailure(error, configFingerprint: fingerprint)
                self?.recordActivationFailure("oss-pull", error: error)
                return
            }

            puller.recordSuccess()
            let candidates = catalog
                .filter { !$0.declaresPendingTranscript && !skipIDs.contains($0.id) }
                .prefix(puller.maxEpisodesPerActivation)

            for item in candidates {
                if Task.isCancelled { break }
                do {
                    let expectedAudio = repairableAudioDigests[item.id]
                    let package = try await puller.pullEpisode(
                        item: item,
                        baseURL: baseURL,
                        token: token,
                        expectedAudioSHA256: expectedAudio
                    )
                    defer { try? FileManager.default.removeItem(at: package.cleanupRoot) }
                    if Task.isCancelled { break }
                    _ = try self?.commitOSSPulledEpisode(package)
                } catch is CancellationError {
                    break
                } catch {
                    self?.recordActivationFailure("oss-pull", error: error)
                }
            }
        }
    }

    @discardableResult
    func commitOSSPulledEpisode(_ package: OSSPulledEpisodePackage) throws -> Bool {
        try serialIngest {
            guard folderURL == nil else { return false }
            let libraryRoot = try ensureDocumentsLibrary()
            let listing = try LibrarySnapshot.path("library.json", in: libraryRoot)
            let fm = FileManager.default
            let before: Data?
            do {
                before = try Data(contentsOf: listing)
            } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
                before = nil
            }
            if before == nil {
                for subdir in ["episodes", "objects"] {
                    let dir = libraryRoot.appendingPathComponent(subdir, isDirectory: true)
                    if let contents = try? fm.contentsOfDirectory(atPath: dir.path), !contents.isEmpty {
                        throw LibrarySnapshotError.invalidCatalog
                    }
                }
            }
            let current = try before.map { try LibrarySnapshot($0) }
            if let current,
               let existingIndex = current.records.firstIndex(where: { $0.catalogID == package.id }) {
                let existingRecord = current.records[existingIndex]
                let existingFolder = try? LibrarySnapshot.path(
                    existingRecord.mediaRevision?.path ?? "episodes/\(package.id)",
                    in: libraryRoot,
                    directory: true
                )
                let existingAudio = existingFolder.flatMap { try? LibrarySnapshot.path("output.mp3", in: $0) }
                if let existingAudio, LocalAudio.isPlayable(existingAudio) {
                    return false
                }
                if let localRevision = existingRecord.mediaRevision,
                   let localAudioHash = localRevision.files["output.mp3"],
                   localAudioHash == package.fileHashes["output.mp3"],
                   let targetFolder = existingFolder {
                    try installPulledObjectFiles(
                        from: package.stagingFolder,
                        into: targetFolder,
                        expectedFiles: localRevision.files
                    )
                    reload()
                    return true
                }
                return false
            }

            guard var incomingRow = try JSONSerialization.jsonObject(with: package.sanitizedRowData) as? [String: Any] else {
                throw LibrarySnapshotError.invalidCatalog
            }
            let dest = try LibrarySnapshot.path("objects/\(package.revision)", in: libraryRoot, directory: true)
            if fm.fileExists(atPath: dest.path) {
                try verifyInstalledObjectFiles(in: dest, expectedFiles: package.fileHashes)
            } else {
                let partial = try LibrarySnapshot.path("objects/\(package.revision).partial", in: libraryRoot, directory: true)
                if fm.fileExists(atPath: partial.path) {
                    try fm.removeItem(at: partial)
                }
                try installPulledObjectFiles(from: package.stagingFolder, into: partial, expectedFiles: package.fileHashes)
                try fm.moveItem(at: partial, to: dest)
            }

            incomingRow["id"] = package.id
            incomingRow["file"] = "output.mp3"
            incomingRow["media_revision"] = [
                "sha256": package.revision,
                "path": "objects/\(package.revision)",
                "previous_path": "",
                "resume_policy": "reset_unmapped",
                "files": package.fileHashes,
            ]

            var proposedRows = current?.rows ?? []
            proposedRows.append(incomingRow)
            var object: [String: Any] = current?.object ?? [:]
            object["episodes"] = proposedRows

            let incoming = try LibrarySnapshot(try LibrarySnapshot.json(object))
            let merged = try incoming.preservingExtensions(from: current)
            let snapshot = try LibrarySnapshot(merged)
            var book = snapshot.transactions
            let generation = (current?.generation ?? 0) + 1
            let parent = before.map(LibrarySnapshot.digest)
            let txId = "oss-pull-\(package.id)-\(String(package.revision.prefix(12)))"
            let requestDigest = LibrarySnapshot.digest(Data("\(txId)|\(package.revision)".utf8))
            let body = try snapshot.stamped(generation: generation, parent: parent, transactions: [:])
            let result = try LibrarySnapshot(body).bodyDigest()
            book[txId] = CatalogTransaction(requestDigest: requestDigest, generation: generation, resultDigest: result)
            let stamped = try snapshot.stamped(generation: generation, parent: parent, transactions: book)
            let _ = try Self.commitSnapshot(stamped, expecting: parent, in: libraryRoot)
            reload()
            return true
        }
    }

    private func verifyInstalledObjectFiles(in folder: URL, expectedFiles: [String: String]) throws {
        for (name, sha) in expectedFiles {
            let file = try LibrarySnapshot.path(name, in: folder)
            let computed = try RevisionAssetVerifier.streamDigestAndSize(of: file)
            guard computed.byteCount > 0, computed.digest == sha else {
                throw LibrarySnapshotError.incompleteRevision
            }
        }
    }

    private func installPulledObjectFiles(
        from sourceFolder: URL,
        into destFolder: URL,
        expectedFiles: [String: String]
    ) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: destFolder, withIntermediateDirectories: true)
        for (name, expectedSHA) in expectedFiles.sorted(by: { $0.key < $1.key }) {
            let src = try LibrarySnapshot.path(name, in: sourceFolder)
            let dst = try LibrarySnapshot.path(name, in: destFolder)
            if fm.fileExists(atPath: dst.path) {
                try fm.removeItem(at: dst)
            }
            try fm.copyItem(at: src, to: dst)
            let computed = try RevisionAssetVerifier.streamDigestAndSize(of: dst)
            guard computed.byteCount > 0, computed.digest == expectedSHA else {
                throw LibrarySnapshotError.incompleteRevision
            }
        }
    }

    private func recordActivationFailure(_ operation: String, error: Error) {
        let nsError = error as NSError
        let sanitizedDescription = (error is OSSClientError) ? "oss_client_error" : nsError.localizedDescription
        var entry: [String: Any] = [
            "operation": operation,
            "occurred_at": ISO8601DateFormatter().string(from: Date()),
            "domain": nsError.domain,
            "code": nsError.code,
            "localized_description": sanitizedDescription,
        ]
        if let error = error as? LibrarySnapshotError {
            entry["error_case"] = String(describing: error)
        } else if let error = error as? PendingTranscript.ReadFailure {
            entry["error_case"] = String(describing: error)
        } else if let error = error as? OSSClientError {
            entry["error_case"] = error.diagnosticCode
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
        let current = try before.map { try LibrarySnapshot($0) }
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
                let inodeBefore = attributes[.systemFileNumber] as? NSNumber
                let modifiedBefore = attributes[.modificationDate] as? Date
                let computed = try RevisionAssetVerifier.streamDigestAndSize(of: file)
                let afterAttributes = try FileManager.default.attributesOfItem(atPath: file.path)
                guard computed.byteCount == size,
                      computed.digest == sha,
                      (afterAttributes[.size] as? NSNumber)?.intValue == size,
                      afterAttributes[.systemFileNumber] as? NSNumber == inodeBefore,
                      afterAttributes[.modificationDate] as? Date == modifiedBefore else {
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
                let computed = try RevisionAssetVerifier.streamDigestAndSize(of: file)
                guard (attributes[.size] as? NSNumber)?.intValue == size,
                      computed.byteCount == size,
                      computed.digest == sha else {
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
            let computed = try RevisionAssetVerifier.streamDigestAndSize(of: to)
            guard computed.byteCount == size, computed.digest == sha else {
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
            var sourceURLs: [String: URL] = [:]
            var fallbackJSONs: [String: Data] = [:]
            var hashes: [String: String] = [:]
            for name in ["output.mp3", "document.json", "source-identity.json", "render-identity.json", "episode.json"] {
                let url = try LibrarySnapshot.path(name, in: folder)
                if FileManager.default.isReadableFile(atPath: url.path) {
                    sourceURLs[name] = url
                }
            }
            guard sourceURLs["output.mp3"] != nil else { throw LibrarySnapshotError.incompleteRevision }
            if let artwork = EpisodeArtwork.file(record.artwork, kind: record.artworkKind) {
                let url = try LibrarySnapshot.path(artwork, in: folder)
                guard LocalAudio.isPlayable(url) else { throw LibrarySnapshotError.incompleteRevision }
                sourceURLs[artwork] = url
            }
            for name in ["document.json", "source-identity.json", "render-identity.json", "episode.json"] where sourceURLs[name] == nil {
                fallbackJSONs[name] = Data("{}".utf8)
            }
            for (name, url) in sourceURLs {
                hashes[name] = try RevisionAssetVerifier.streamDigestAndSize(of: url).digest
            }
            for (name, data) in fallbackJSONs {
                hashes[name] = LibrarySnapshot.digest(data)
            }
            let fileLines = hashes.sorted(by: { $0.key < $1.key }).map { "  \"\($0.key)\": \"\($0.value)\"" }
            let revision = LibrarySnapshot.digest(Data(("{\n" + fileLines.joined(separator: ",\n") + "\n}\n").utf8))
            let destPartial = try LibrarySnapshot.path("objects/\(revision).partial", in: libraryRoot, directory: true)
            if FileManager.default.fileExists(atPath: destPartial.path) {
                try FileManager.default.removeItem(at: destPartial)
            }
            try FileManager.default.createDirectory(at: destPartial, withIntermediateDirectories: true)
            var count = 0
            for name in hashes.keys.sorted() {
                if fault == .midInstall, count == 1 { throw LibrarySnapshotError.rejectedIngest }
                let dst = destPartial.appendingPathComponent(name)
                if let src = sourceURLs[name] {
                    try FileManager.default.copyItem(at: src, to: dst)
                } else if let data = fallbackJSONs[name] {
                    try data.write(to: dst, options: .atomic)
                }
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
