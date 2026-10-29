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
    @Published private(set) var title = ""
    @Published private(set) var isPlaying = false
    @Published private(set) var current: Double = 0
    @Published private(set) var duration: Double = 0
    @Published var speed: ListenSpeed
    @Published var errorText: String?

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
        if !force, currentID == episode.id, errorText == nil, player.currentItem != nil {
            // Cover may have arrived after the first open — refresh Now Playing artwork.
            artist = Self.showName(in: episode.title)
            nowArtwork = ArtworkImage.nowPlayingItem(for: episode)
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
        currentID = episode.id
        title = episode.title
        artist = Self.showName(in: episode.title)
        nowArtwork = ArtworkImage.nowPlayingItem(for: episode)
        errorText = nil
        let start = max(0, resumeAt)
        current = start
        duration = episode.durationSec ?? 0
        lastSaved = -1
        guard LocalAudio.isPlayable(episode.audioURL) else {
            try? FileManager.default.startDownloadingUbiquitousItem(at: episode.audioURL)
            errorText = "音频还在从 iCloud 下载，稍后再播放。"
            player.replaceCurrentItem(with: nil)
            isPlaying = false
            updateNowPlaying()
            return
        }
        let item = AVPlayerItem(url: episode.audioURL)
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
        if duration > 1, current >= duration - 0.5 {
            seek(to: 0, autoplay: true)
            return
        }
        try? AVAudioSession.sharedInstance().setActive(true)
        player.playImmediately(atRate: speed.rate)
        isPlaying = true
        updateNowPlaying()
    }

    func pause() {
        playWhenReady = false
        player.pause()
        isPlaying = false
        checkpoint()
        updateNowPlaying()
    }

    func skip(_ delta: Double) {
        seek(to: current + delta, autoplay: isPlaying)
    }

    func seek(to seconds: Double, autoplay: Bool? = nil) {
        let upper = duration > 0 ? duration : max(seconds, 0)
        let targetSeconds = min(max(0, seconds), upper)
        let shouldPlay = autoplay ?? isPlaying
        pendingResume = nil
        resumeSeekID += 1
        resumeSeekInFlight = false
        player.seek(
            to: CMTime(seconds: targetSeconds, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.dropPrematureZero = false
                self.positionTrusted = true
                self.current = targetSeconds
                self.checkpoint()
                if shouldPlay {
                    self.player.playImmediately(atRate: self.speed.rate)
                    self.isPlaying = true
                }
                self.updateNowPlaying()
            }
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
        guard positionTrusted, let currentID else { return }
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
        if lastSaved < 0 || abs(seconds - lastSaved) >= 5 {
            checkpoint()
        }
        // Only nudge elapsed/rate — rewriting full Now Playing (with artwork) every 0.5s
        // can leave the lock-screen artwork blank on recent iOS.
        updateNowPlayingProgress()
    }

    private func handleEnded() {
        isPlaying = false
        if duration > 0 { current = duration }
        checkpoint()
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
        guard currentID != nil else {
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
        guard currentID != nil else { return }
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
