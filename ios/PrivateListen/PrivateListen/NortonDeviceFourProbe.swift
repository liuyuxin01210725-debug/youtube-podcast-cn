#if DEBUG || os(macOS)
import Foundation
#if os(iOS) && !targetEnvironment(simulator)
import UIKit
#endif

/// Main-track acceptance for the approved, already ingested short sample.
@MainActor
enum NortonDeviceFourProbe {
    static let episodeID = "fd4c6200b01540d992b5a9e39784eb2e"

    static func target(in library: LibraryStore, root: URL) -> Episode? {
        let required: Set<String> = ["output.mp3", "episode.json", "cover.png", "document.json",
                                     "source-identity.json", "render-identity.json"]
        guard library.readingLocalCopyOnly,
              let episode = library.episodes.first(where: { $0.id == episodeID }),
              let revision = episode.mediaRevision,
              let files = episode.revisionFiles, required.isSubset(of: files),
              let audio = try? LibrarySnapshot.path("objects/\(revision)/output.mp3", in: root),
              audio.standardizedFileURL == episode.audioURL.standardizedFileURL,
              episode.artworkFile == "cover.png", episode.isPlayableNow else { return nil }
        return episode
    }

    static func checkpointMatches(_ episode: Episode, in library: LibraryStore, defaults: UserDefaults,
                                  playhead: Double, wasFinished: Bool) -> Bool {
        let saved = library.resumeSeconds(for: episode)
        guard let card = library.continueEntries(in: library.episodes).first, card.id == episode.id,
              let data = defaults.data(forKey: "PrivateListen.progress"),
              let marks = try? JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: data),
              let mark = marks[episode.mediaKey],
              let persisted = wasFinished ? mark.replaySeconds : mark.seconds else { return false }
        return saved > 0 && abs(saved - playhead) < 0.75 && abs(card.seconds - saved) < 0.01
            && abs(persisted - saved) < 0.01 && mark.finished == wasFinished
            && library.isFinished(episode) == wasFinished
    }

    #if os(iOS) && !targetEnvironment(simulator)
    private static var started = false

    static func runIfNeeded(player: PlayerModel, library: LibraryStore) async {
        let arguments = ProcessInfo.processInfo.arguments
        guard arguments.contains("-NortonDeviceFourProbe"), !started else { return }
        started = true
        let runID = arguments.firstIndex(of: "-NortonProbeRunID").flatMap {
            arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil
        } ?? UUID().uuidString
        guard let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else { return }
        let root = documents.appendingPathComponent("中文播客私人库", isDirectory: true)
        let reportURL = documents.appendingPathComponent("short-sample-main-track-probe.json")
        var checks: [String: Bool] = [:]
        var seconds: [String: Double] = [:]
        func persist(done: Bool) {
            let report: [String: Any] = [
                "schema": "shengjian.short-sample-main-track.probe.v1", "run_id": runID,
                "episode_id": episodeID, "device": "physical", "done": done,
                "device_checks_pass": done && checks["P-Play"] == true && checks["P-Continue"] == true,
                "checks": checks, "seconds": seconds, "ui_taps_performed": false,
            ]
            do {
                let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: reportURL, options: .atomic)
            } catch {
                NSLog("Short sample probe receipt write failed")
            }
        }
        persist(done: false)
        defer { persist(done: true) }

        let idleBefore = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = idleBefore }
        do {
            // The foreground ingest and LibraryView.onAppear may still be running.
            for _ in 0..<50 {
                if player.onProgress != nil, target(in: library, root: root) != nil { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            guard let productionProgress = player.onProgress, let episode = target(in: library, root: root),
                  player.currentID == nil, player.currentBookReference == nil else {
                checks["setup"] = false
                return
            }
            checks["setup"] = true
            let wasFinished = library.isFinished(episode)
            let speedBefore = player.speed
            var writes = 0
            var paused = false
            player.onProgress = { id, seconds, duration, finished in
                if id == episode.id { writes += 1 }
                return productionProgress(id, seconds, duration, finished)
            }
            player.speed = .one
            defer {
                if !paused { player.pause() }
                player.speed = speedBefore
                player.onProgress = productionProgress
            }

            library.acknowledgeContinue(episode)
            player.open(episode, resumeAt: library.resumeSeconds(for: episode), autoplay: !wasFinished)
            if wasFinished { player.resume() }
            let process = ProcessInfo.processInfo
            let deadline = process.systemUptime + 8
            while !player.pointListenProbeClock.playing, player.errorText == nil, process.systemUptime < deadline {
                try await Task.sleep(for: .milliseconds(50))
            }
            let start = player.pointListenProbeClock
            guard start.playing, start.seconds.isFinite, player.isCurrent(episode), !player.isClipPlayback else {
                checks["P-Play"] = false
                return
            }

            let startedAt = process.systemUptime
            try await Task.sleep(for: .milliseconds(4_100))
            let heard = player.pointListenProbeClock
            let wall = process.systemUptime - startedAt
            let advanced = heard.seconds - start.seconds
            player.pause()
            paused = true
            let writesBeforeExit = writes
            player.checkpoint() // PlayerView.onDisappear uses the same checkpoint.
            checks["P-Play"] = (3...5).contains(wall) && (3...5).contains(advanced)
                && heard.playing && player.isCurrent(episode) && !player.isClipPlayback
                && !player.pointListenProbeClock.playing
            checks["P-Continue"] = writes > writesBeforeExit && checkpointMatches(
                episode, in: library, defaults: .standard, playhead: player.current, wasFinished: wasFinished
            )
            seconds = ["wall": wall, "saved": library.resumeSeconds(for: episode)]
            if advanced.isFinite { seconds["advanced"] = advanced }
        } catch {
            checks["interrupted"] = false
        }
    }
    #endif
}
#endif
