import AVFoundation
import Combine
import MediaPlayer
import UIKit

enum ListenSpeed: String, CaseIterable, Identifiable {
    case one = "1"
    case quarter = "1.25"
    case half = "1.5"
    case two = "2"

    var id: String { rawValue }

    var rate: Float {
        switch self {
        case .one: return 1
        case .quarter: return 1.25
        case .half: return 1.5
        case .two: return 2
        }
    }

    var label: String {
        switch self {
        case .one: return "1×"
        case .quarter: return "1.25×"
        case .half: return "1.5×"
        case .two: return "2×"
        }
    }

    static func stored(_ raw: String?) -> ListenSpeed {
        guard let raw, let match = ListenSpeed(rawValue: raw) else { return .one }
        return match
    }
}

final class PlayerModel: ObservableObject {
    @Published private(set) var currentID: String?
    @Published private(set) var currentBookReference: BookPlaybackReference?
    var onBookProgress: ((BookPlaybackReference, Double, Double, Double, Bool) -> Void)?
    private var bookObservedClock: Double?
    @Published private(set) var title = ""
    @Published private(set) var isPlaying = false
    @Published private(set) var current: Double = 0
    @Published private(set) var duration: Double = 0
    @Published var speed: ListenSpeed
    @Published var errorText: String?
    /// Full-screen PlayerView is up; the library mini bar steps aside.
    @Published var fullPlayerOpen = false

    var onProgress: ((String, Double, Double) -> Void)?

    private let player = AVPlayer()
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var interruptionObserver: NSObjectProtocol?
    private var itemWatchers = Set<AnyCancellable>()
    private var lastSaved: Double = -1
    /// Ignore playhead ticks until a resume seek has landed. Otherwise the observer
    /// writes 0 over the saved episode and the next launch starts over.
    private var positionTrusted = false
    private var pendingResume: Double?
    private var playWhenReady = false
    private var dropPrematureZero = false
    private var openGeneration = 0
    private var resumeSeekID = 0
    private var resumeSeekInFlight = false
    private let speedKey = "PrivateListen.speed"
    private var nowArtwork: MPMediaItemArtwork?
    private var artist = "声间"
    /// When set, auto-pause once playhead reaches this time (output.mp3 axis).
    private var clipStopAt: Double?
    /// While true, checkpoint/onProgress must not write ListenMark (§13.2 clip path).
    private var clipSuppressListenMark = false
    private var jumpProgress = JumpContinueProgress()

    /// 「听这段」 owns the playhead. The continue pill seeks back to ListenMark instead of toggling.
    var isClipPlayback: Bool { clipSuppressListenMark }

    #if DEBUG
    /// Read-only device evidence; never substitutes a requested seek for AVPlayer's clock.
    var pointListenProbeClock: (seconds: Double, playing: Bool) {
        (player.currentTime().seconds, player.timeControlStatus == .playing)
    }
    #endif

    init() {
        speed = ListenSpeed.stored(UserDefaults.standard.string(forKey: speedKey))
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio)
        UIApplication.shared.beginReceivingRemoteControlEvents()
        installRemoteCommands()
        timeObserver = player.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            self?.handle(time: time.seconds)
        }
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { [weak self] note in
            self?.handleInterruption(note)
        }
    }

    func open(_ episode: Episode, resumeAt: Double, force: Bool = false) {
        openMedia(podcastID: episode.id, book: nil, mediaTitle: episode.title, mediaArtist: Self.showName(in: episode.title), url: episode.audioURL, artwork: ArtworkImage.nowPlayingItem(for: episode), mediaDuration: episode.durationSec ?? 0, resumeAt: resumeAt, force: force)
    }

    func openBook(reference: BookPlaybackReference, title: String, bookTitle: String, url: URL, duration: Double, resumeAt: Double) {
        openMedia(podcastID: nil, book: reference, mediaTitle: title, mediaArtist: bookTitle, url: url, artwork: nil, mediaDuration: duration, resumeAt: resumeAt, force: false)
        // A Continue tap must resume an already-open paused chapter too.
        resume()
    }

    private func openMedia(podcastID: String?, book: BookPlaybackReference?, mediaTitle: String, mediaArtist: String, url: URL, artwork: MPMediaItemArtwork?, mediaDuration: Double, resumeAt: Double, force: Bool) {
        if !force, currentID == podcastID && currentBookReference == book, errorText == nil, player.currentItem != nil {
            // Cover may have arrived after the first open — refresh Now Playing artwork.
            artist = mediaArtist
            nowArtwork = artwork
            updateNowPlaying()
            return
        }
        if positionTrusted { checkpoint() }
        openGeneration += 1
        let generation = openGeneration
        pendingResume = nil
        playWhenReady = false
        dropPrematureZero = false
        positionTrusted = false
        resumeSeekInFlight = false
        resumeSeekID += 1
        jumpProgress.reset()
        clearClipPlayback(restoreListenMarkWrites: true)
        currentID = podcastID
        currentBookReference = book
        bookObservedClock = nil
        self.title = mediaTitle
        artist = mediaArtist
        nowArtwork = artwork
        errorText = nil
        let start = max(0, resumeAt)
        current = start
        duration = mediaDuration
        lastSaved = -1
        guard LocalAudio.isPlayable(url) else {
            try? FileManager.default.startDownloadingUbiquitousItem(at: url)
            errorText = "音频还在从 iCloud 下载，稍后再播放。"
            player.replaceCurrentItem(with: nil)
            isPlaying = false
            updateNowPlaying()
            return
        }
        let item = AVPlayerItem(url: url)
        itemWatchers.removeAll()
        if start > 1 {
            pendingResume = start
            playWhenReady = true
        } else {
            positionTrusted = true
            playWhenReady = true
        }
        item.publisher(for: \.status)
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                guard let self, self.openGeneration == generation else { return }
                if status == .failed {
                    self.pendingResume = nil
                    self.playWhenReady = false
                    self.errorText = "这段音频打不开。"
                    self.isPlaying = false
                    self.updateNowPlaying()
                } else if status == .readyToPlay {
                    self.applyPendingResume(generation: generation)
                }
            }
            .store(in: &itemWatchers)
        item.publisher(for: \.duration)
            .receive(on: RunLoop.main)
            .sink { [weak self] time in
                let seconds = time.seconds
                guard let self, self.openGeneration == generation, seconds.isFinite, seconds > 0 else { return }
                self.duration = seconds
                self.updateNowPlaying()
            }
            .store(in: &itemWatchers)
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
        }
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self] _ in
            guard let self, self.openGeneration == generation else { return }
            self.handleEnded()
        }
        player.replaceCurrentItem(with: item)
        if item.status == .readyToPlay {
            applyPendingResume(generation: generation)
        }
        if start <= 1 {
            resume()
        } else {
            isPlaying = false
            updateNowPlaying()
        }
    }

    private func applyPendingResume(generation: Int) {
        guard openGeneration == generation, let start = pendingResume else { return }
        pendingResume = nil
        resumeSeekID += 1
        resumeSeekInFlight = true
        let seekID = resumeSeekID
        let target = CMTime(seconds: start, preferredTimescale: 600)
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            DispatchQueue.main.async {
                self?.finishResumeSeek(start: start, generation: generation, seekID: seekID, allowRetry: true)
            }
        }
    }

    private func finishResumeSeek(start: Double, generation: Int, seekID: Int, allowRetry: Bool) {
        guard openGeneration == generation, resumeSeekID == seekID else { return }
        let landed = player.currentTime().seconds
        if allowRetry, start > 1, !landed.isFinite || landed < 1 {
            let target = CMTime(seconds: start, preferredTimescale: 600)
            player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
                DispatchQueue.main.async {
                    self?.finishResumeSeek(start: start, generation: generation, seekID: seekID, allowRetry: false)
                }
            }
            return
        }
        resumeSeekInFlight = false
        if landed.isFinite, landed >= 1 {
            current = landed
        } else {
            current = start
        }
        dropPrematureZero = true
        positionTrusted = true
        if playWhenReady {
            resume()
        } else {
            updateNowPlaying()
        }
    }

    func toggle() {
        if isPlaying {
            pause()
        } else {
            resume()
        }
    }

    func resume() {
        guard errorText == nil else { return }
        if pendingResume != nil || resumeSeekInFlight {
            playWhenReady = true
            return
        }
        guard player.currentItem != nil else { return }
        // AVPlayer playImmediately is a no-op when the item is already at the end.
        // Restart from 0 so a finished isolate (e.g. 0:08/0:08) still plays on tap.
        if PlayerPlaybackPolicy.shouldRestartFromBeginning(
            current: current,
            duration: duration,
            liveClock: player.currentTime().seconds
        ) {
            seek(to: 0, autoplay: true)
            return
        }
        try? AVAudioSession.sharedInstance().setActive(true)
        player.playImmediately(atRate: speed.rate)
        isPlaying = true
        updateNowPlaying()
    }

    func pause() {
        _ = jumpProgress.observe(seconds: player.currentTime().seconds, isPlaying: player.timeControlStatus == .playing)
        playWhenReady = false
        player.pause()
        isPlaying = false
        let suppress = clipSuppressListenMark
        clearClipPlayback(restoreListenMarkWrites: !suppress)
        if !suppress {
            checkpoint()
        }
        updateNowPlaying()
    }

    func skip(_ delta: Double) {
        seek(to: current + delta, autoplay: isPlaying)
    }

    /// Slice A「听这段」：按最小窗口扩窗后 seek → 播 → 到终点暂停。Does not write ListenMark (§13.2).
    ///
    /// 产品规则（听这段最小窗口 · 2026-10-30）：
    /// - `clipStart = max(0, t_start - leadIn)`，leadIn 默认 2.5s，给一点上文。
    /// - 有合法 `t_end`：`clipEnd = max(t_end, t_start + minDuration)`，minDuration 默认 28s，
    ///   避免真抽短 span（常 <10s）只播十几字就停。
    /// - 缺 `t_end` 或 `t_end ≤ t_start`：回退 `t_start + fallbackSeconds`（默认 = minDuration）。
    /// - softCap 默认 60s（相对 clipStart）：偶发 150s 长 span 不强制听到底；用户可自己拖进度。
    func playClip(
        from start: Double,
        to end: Double?,
        leadIn: Double = 2.5,
        minDuration: Double = 28,
        softCap: Double = 60,
        fallbackSeconds: Double = 28
    ) {
        let window = Self.resolveClipWindow(
            start: start,
            end: end,
            leadIn: leadIn,
            minDuration: minDuration,
            softCap: softCap,
            fallbackSeconds: fallbackSeconds
        )
        clipSuppressListenMark = true
        clipStopAt = window.end
        seek(to: window.start, autoplay: true, clearClip: false)
    }

    /// 返回扩窗后的 (clipStart, clipEnd)。`resolveClipStop` 取 `.end`，便于单测。
    static func resolveClipWindow(
        start: Double,
        end: Double?,
        leadIn: Double = 2.5,
        minDuration: Double = 28,
        softCap: Double = 60,
        fallbackSeconds: Double = 28
    ) -> (start: Double, end: Double) {
        let clipStart = max(0, start - max(0, leadIn))
        let rawEnd: Double
        if let end, end > start {
            rawEnd = max(end, start + max(0, minDuration))
        } else {
            rawEnd = start + max(1, fallbackSeconds)
        }
        var clipEnd = rawEnd
        let cap = max(1, softCap)
        if clipEnd - clipStart > cap {
            clipEnd = clipStart + cap
        }
        if clipEnd <= clipStart {
            clipEnd = clipStart + max(1, fallbackSeconds)
        }
        return (clipStart, clipEnd)
    }

    static func resolveClipStop(
        start: Double,
        end: Double?,
        leadIn: Double = 2.5,
        minDuration: Double = 28,
        softCap: Double = 60,
        fallbackSeconds: Double = 28
    ) -> Double {
        resolveClipWindow(
            start: start,
            end: end,
            leadIn: leadIn,
            minDuration: minDuration,
            softCap: softCap,
            fallbackSeconds: fallbackSeconds
        ).end
    }

    /// Main point-listen path: exact final-audio seconds, no clip window, no mark on click.
    @discardableResult
    func jumpContinue(
        episode: Episode, row: ListenViewpoint, index: PointListenIndex,
        packet: ExtractionPacket, audio: PointListenAudio
    ) -> Bool {
        guard audio.url == episode.audioURL, audio.url.lastPathComponent == "output.mp3", audio.isCurrent,
              PointListening.refusal(row: row, index: index, packet: packet, episodeID: episode.id,
                  audioHash: audio.sha256, duration: audio.duration) == nil,
              let start = row.tStart else { return false }
        open(episode, resumeAt: 0)
        guard errorText == nil else { return false }
        duration = audio.duration
        seek(to: start, autoplay: true, waitForPlaybackAdvance: true)
        return true
    }

    func seek(to seconds: Double, autoplay: Bool? = nil, clearClip: Bool = true, waitForPlaybackAdvance: Bool = false) {
        guard seconds.isFinite, player.currentItem != nil else { return }
        if clearClip {
            clearClipPlayback(restoreListenMarkWrites: true)
        }
        jumpProgress.reset()
        bookObservedClock = nil
        let upper = duration > 0 ? duration : max(seconds, 0)
        let targetSeconds = min(max(0, seconds), upper)
        playWhenReady = autoplay ?? isPlaying
        player.pause()
        isPlaying = false
        positionTrusted = false
        pendingResume = nil
        resumeSeekID += 1
        resumeSeekInFlight = true
        let seekID = resumeSeekID
        let generation = openGeneration
        player.seek(
            to: CMTime(seconds: targetSeconds, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, self.openGeneration == generation, self.resumeSeekID == seekID else { return }
                self.resumeSeekInFlight = false
                let landed = self.player.currentTime().seconds
                guard finished, landed.isFinite, landed >= 0, abs(landed - targetSeconds) < 1 else {
                    self.playWhenReady = false
                    self.errorText = "未能定位到这一段，请重新打开音频。"
                    self.updateNowPlaying()
                    return
                }
                self.dropPrematureZero = false
                self.positionTrusted = true
                self.current = PlayerPlaybackPolicy.progressAfterSeek(target: targetSeconds, liveClock: landed)
                if waitForPlaybackAdvance {
                    self.jumpProgress.didLand(at: landed)
                    self.lastSaved = -1
                } else if !self.clipSuppressListenMark {
                    self.lastSaved = self.current
                    if let ref = self.currentBookReference {
                        self.onBookProgress?(ref, self.current, self.duration, 0, false)
                    } else if let id = self.currentID {
                        self.onProgress?(id, self.current, self.duration)
                    }
                }
                if self.playWhenReady {
                    try? AVAudioSession.sharedInstance().setActive(true)
                    self.player.playImmediately(atRate: self.speed.rate)
                    self.isPlaying = true
                }
                self.updateNowPlaying()
            }
        }
    }

    private func clearClipPlayback(restoreListenMarkWrites: Bool) {
        clipStopAt = nil
        if restoreListenMarkWrites {
            clipSuppressListenMark = false
        }
    }

    func setSpeed(_ newSpeed: ListenSpeed) {
        speed = newSpeed
        UserDefaults.standard.set(newSpeed.rawValue, forKey: speedKey)
        if isPlaying {
            player.playImmediately(atRate: newSpeed.rate)
        }
        updateNowPlaying()
    }

    func checkpoint() {
        guard !clipSuppressListenMark else { return }
        guard jumpProgress.observe(seconds: player.currentTime().seconds, isPlaying: player.timeControlStatus == .playing) else { return }
        if let ref = currentBookReference {
            guard positionTrusted else { return }
            current = ContinueListening.playheadToPersist(observed: current, liveClock: player.currentTime().seconds)
            onBookProgress?(ref, current, duration, 0, false)
            return
        }
        guard positionTrusted, let currentID else { return }
        // Observer ticks are 0.5s apart. Leaving must store the clock the
        // listener reached, including a short play that has not crossed 5s.
        current = ContinueListening.playheadToPersist(
            observed: current,
            liveClock: player.currentTime().seconds
        )
        lastSaved = current
        onProgress?(currentID, current, duration)
    }

    private func handle(time seconds: Double) {
        guard positionTrusted, seconds.isFinite, seconds >= 0 else { return }
        if dropPrematureZero, seconds < 1 { return }
        dropPrematureZero = false
        current = seconds
        if let itemDuration = player.currentItem?.duration.seconds, itemDuration.isFinite, itemDuration > 0 {
            duration = itemDuration
        }
        isPlaying = player.timeControlStatus == .playing
        if let ref = currentBookReference {
            let delta = BookProgressPolicy.actualAdvance(previous: bookObservedClock, current: seconds, playing: isPlaying, rate: speed.rate)
            bookObservedClock = seconds
            if delta > 0 { onBookProgress?(ref, seconds, duration, delta, false) }
            updateNowPlayingProgress()
            return
        }
        guard jumpProgress.observe(seconds: seconds, isPlaying: isPlaying) else { return }
        if let stop = clipStopAt, seconds >= stop {
            // Clip end: pause in place; do not write ListenMark (§13.2).
            playWhenReady = false
            player.pause()
            isPlaying = false
            current = stop
            clearClipPlayback(restoreListenMarkWrites: false)
            updateNowPlaying()
            return
        }
        if lastSaved < 0 || abs(seconds - lastSaved) >= 5 {
            checkpoint()
        }
        // Only nudge elapsed/rate — rewriting full Now Playing (with artwork) every 0.5s
        // can leave the lock-screen artwork blank on recent iOS.
        updateNowPlayingProgress()
    }

    private func handleEnded() {
        let suppress = clipSuppressListenMark
        clearClipPlayback(restoreListenMarkWrites: !suppress)
        isPlaying = false
        playWhenReady = false
        if let itemDuration = player.currentItem?.duration.seconds, itemDuration.isFinite, itemDuration > 0 {
            duration = itemDuration
        }
        if duration > 0 {
            current = duration
        }
        if let ref = currentBookReference {
            onBookProgress?(ref, current, duration, 0, true)
        } else if !suppress {
            checkpoint()
        }
        updateNowPlaying()
    }

    private func handleInterruption(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        if type == .began {
            isPlaying = false
            checkpoint()
            updateNowPlaying()
        }
    }

    private func installRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async { self?.resume() }
            return .success
        }
        center.pauseCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async { self?.pause() }
            return .success
        }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async { self?.toggle() }
            return .success
        }
        center.skipForwardCommand.preferredIntervals = [NSNumber(value: 15)]
        center.skipBackwardCommand.preferredIntervals = [NSNumber(value: 15)]
        center.skipForwardCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async { self?.skip(15) }
            return .success
        }
        center.skipBackwardCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async { self?.skip(-15) }
            return .success
        }
        center.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let position = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            DispatchQueue.main.async { self?.seek(to: position.positionTime) }
            return .success
        }
    }

    private func updateNowPlaying() {
        guard currentID != nil || currentBookReference != nil else {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            return
        }
        let topic = Self.topicName(in: title)
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: topic,
            MPMediaItemPropertyArtist: artist,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: current,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? Double(speed.rate) : 0,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
        ]
        if duration > 0 {
            info[MPMediaItemPropertyPlaybackDuration] = duration
        }
        if let nowArtwork {
            info[MPMediaItemPropertyArtwork] = nowArtwork
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func updateNowPlayingProgress() {
        guard currentID != nil || currentBookReference != nil else { return }
        guard var info = MPNowPlayingInfoCenter.default().nowPlayingInfo, !info.isEmpty else {
            updateNowPlaying()
            return
        }
        info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = current
        info[MPNowPlayingInfoPropertyPlaybackRate] = isPlaying ? Double(speed.rate) : 0
        if duration > 0 {
            info[MPMediaItemPropertyPlaybackDuration] = duration
        }
        if let nowArtwork {
            info[MPMediaItemPropertyArtwork] = nowArtwork
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private static func topicName(in title: String) -> String {
        guard let range = title.range(of: " · ") else { return title }
        let topic = title[..<range.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
        return topic.isEmpty ? title : String(topic)
    }

    private static func showName(in title: String) -> String {
        guard let range = title.range(of: " · ") else { return "声间" }
        let show = title[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return show.isEmpty ? "声间" : String(show)
    }
}
