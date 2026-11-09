#if DEBUG && !targetEnvironment(simulator)
import CryptoKit
import Foundation
import UIKit

/// Explicitly opted-in physical-device acceptance. No fixture, publishing, extraction,
/// note editing, or replacement progress writer. Only real Norton playback changes its mark.
@MainActor
enum NortonDeviceFourProbe {
    private static var started = false
    static let episodeID = "42786039aaf346a1bf851613199e260a"

    static func runIfNeeded(player: PlayerModel, library: LibraryStore) async {
        let arguments = ProcessInfo.processInfo.arguments
        guard arguments.contains("-NortonDeviceFourProbe"), !started else { return }
        started = true
        let runID = arguments.firstIndex(of: "-NortonProbeRunID").flatMap {
            arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil
        } ?? UUID().uuidString
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let reportURL = documents.appendingPathComponent("norton-device-four-probe.json")
        let startedAt = Date()
        var checks: [[String: Any]] = []
        var samples: [[String: Any]] = []
        func check(_ group: String, _ name: String, _ pass: Bool, _ details: [String: Any] = [:]) {
            checks.append(details.merging(["group": group, "name": name, "pass": pass]) { _, new in new })
        }
        func wait(_ seconds: Double) async {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        }
        func hash(_ data: Data) -> String {
            SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        }
        func persist(done: Bool) {
            let groups = ["P-Play", "P-Continue", "P-Merge"].reduce(into: [String: Bool]()) { result, group in
                let items = checks.filter { $0["group"] as? String == group }
                result[group] = !items.isEmpty && items.allSatisfy { $0["pass"] as? Bool == true }
            }
            let report: [String: Any] = [
                "schema": "shengjian.norton-device-four.probe.v1", "run_id": runID,
                "device": "physical", "model": UIDevice.current.model,
                "system_version": UIDevice.current.systemVersion,
                "started_at": ISO8601DateFormatter().string(from: startedAt),
                "updated_at": ISO8601DateFormatter().string(from: Date()),
                "episode_id": episodeID, "done": done,
                "device_checks_pass": done && groups.values.allSatisfy { $0 }
                    && checks.allSatisfy { $0["pass"] as? Bool == true },
                "groups": groups, "checks": checks, "samples": samples,
                "P-ListenNote": ["validation": "host source assertion required against installed source hashes",
                    "automatic_note_write": false],
                "merge_evidence": "../device-file-verification.json",
                "merge_scope": "单集已落盘、library.json 未整库盖、基线 52 保留",
                "ui_taps_performed": false, "probe_kind": "equivalent self-driven real playback",
                "semantic_landing_verified": false, "claim_phone_listenable": false
            ]
            do {
                let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: reportURL, options: .atomic)
            } catch {
                NSLog("NortonDeviceFourProbe receipt write failed: %@", String(describing: error))
            }
        }
        persist(done: false) // A new run can never inherit an earlier successful receipt.
        let idleBefore = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = idleBefore }
        do {
            // LibraryView installs the production onProgress callback on appearance.
            for _ in 0..<50 {
                if player.onProgress != nil { break }
                await wait(0.1)
            }
            guard let productionProgress = player.onProgress,
                  let episode = library.episodes.first(where: { $0.id == episodeID }) else {
                throw NSError(domain: "NortonDeviceFourProbe", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "Norton or production progress callback unavailable"])
            }
            let root = documents.appendingPathComponent("中文播客私人库")
            let folder = root.appendingPathComponent("episodes").appendingPathComponent(episodeID)
            let catalogURL = root.appendingPathComponent("library.json")
            let catalogBefore = try Data(contentsOf: catalogURL)
            let catalog = try JSONSerialization.jsonObject(with: catalogBefore) as? [String: Any]
            let records = catalog?["episodes"] as? [[String: Any]] ?? []
            let expectedFiles = ["output.mp3", "cover.png", "transcript_zh.json", "extract.live.json",
                "extract.lkg.json", "extract.json", "point-listen.json", "episode.json"]
            check("P-Merge", "local_Norton_and_52_baseline", records.count == 52
                && library.episodes.count == 52
                && episode.audioURL.standardizedFileURL == folder.appendingPathComponent("output.mp3").standardizedFileURL
                && expectedFiles.allSatisfy { FileManager.default.fileExists(atPath: folder.appendingPathComponent($0).path) },
                ["catalog_count": records.count, "visible_count": library.episodes.count,
                 "library_sha256": hash(catalogBefore), "audio_path": episode.audioURL.path])
            let noteURL = documents.appendingPathComponent(ListenNoteStore.fileName)
            let notesBefore = try? Data(contentsOf: noteURL)
            let packet = try JSONDecoder().decode(ExtractionPacket.self,
                from: Data(contentsOf: folder.appendingPathComponent("extract.json")))
            let index = try JSONDecoder().decode(PointListenIndex.self,
                from: Data(contentsOf: folder.appendingPathComponent(PointListening.indexFileName)))
            let audio = try await PointListenAudio.load(episode.audioURL)
            let rows = PointListening.rows(packet: packet, index: index)
            guard rows.count == 7, audio.sha256 == "fcb337634866181e6107eb6cb519006fe9102c756b35f83bb8d1410a04749f25" else {
                throw CocoaError(.fileReadCorruptFile)
            }
            // These real, unmodified windows can be crossed within a 3–5 second listen.
            let selected = ["claim-s1", "claim-i9"].compactMap { id in rows.first { $0.id == id } }
            guard selected.count == 2, selected.allSatisfy({ row in
                guard let start = row.tStart, let end = row.tEnd else { return false }
                return start.isFinite && end.isFinite && start >= 0 && end > start && end - start <= 3.6
            }) else { throw CocoaError(.fileReadCorruptFile) }
            var writes: [[String: Any]] = []
            player.onProgress = { id, seconds, duration in
                writes.append(["id": id, "seconds": seconds, "duration": duration])
                productionProgress(id, seconds, duration) // Real LibraryView writer, not a probe substitute.
            }
            let speedBefore = player.speed
            player.speed = .one // Runtime only; do not change the listener's saved speed.
            defer {
                player.pause()
                player.speed = speedBefore
                player.onProgress = productionProgress
            }
            library.acknowledgeContinue(episodeID) // Same as PlayerView.onAppear.
            for row in selected {
                let beforeWrites = writes.count
                let beforeFinished = library.isFinished(episodeID)
                let beforeMark = UserDefaults.standard.data(forKey: "PrivateListen.progress")
                let accepted = player.jumpContinue(episode: episode, row: row, index: index, packet: packet, audio: audio)
                check("P-Play", "\(row.id)_click_does_not_write_completed", accepted && writes.count == beforeWrites
                    && library.isFinished(episodeID) == beforeFinished
                    && UserDefaults.standard.data(forKey: "PrivateListen.progress") == beforeMark)
                var landed = player.pointListenProbeClock
                let seekingAt = Date()
                while Date().timeIntervalSince(seekingAt) < 8 {
                    landed = player.pointListenProbeClock
                    if landed.playing, abs(landed.seconds - row.tStart!) < 0.75 { break }
                    await wait(0.05)
                }
                let landingOK = landed.playing && abs(landed.seconds - row.tStart!) < 0.75
                check("P-Play", "\(row.id)_real_AVPlayer_landing", landingOK,
                    ["clock": landed.seconds, "t_start": row.tStart!, "delta_seconds": landed.seconds - row.tStart!])
                let listeningAt = Date()
                await wait(4.2)
                let heard = player.pointListenProbeClock
                let wall = Date().timeIntervalSince(listeningAt)
                check("P-Play", "\(row.id)_real_play_crosses_t_end", landingOK && heard.playing
                    && player.isPlaying && !player.isClipPlayback && wall >= 3 && wall <= 5
                    && heard.seconds - landed.seconds >= 3 && heard.seconds > row.tEnd!,
                    ["wall_seconds": wall, "advanced_seconds": heard.seconds - landed.seconds,
                     "playhead": heard.seconds, "t_end": row.tEnd!])
                player.checkpoint() // Exact PlayerView.onDisappear / scene inactive persistence path.
                player.pause()
                await wait(0.25) // LibraryView's production callback persists on a MainActor task.
                let saved = library.resumeSeconds(for: episodeID)
                let card = library.continueEntries(in: library.episodes).first
                let persisted = UserDefaults.standard.data(forKey: "PrivateListen.progress")
                    .flatMap { try? JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: $0) }
                check("P-Continue", "\(row.id)_checkpoint_exit_Continue_and_persisted_ListenMark",
                    card?.id == episodeID && abs((card?.seconds ?? -1) - player.current) < 0.75
                    && abs(saved - player.current) < 0.75 && saved >= row.tStart! + 3
                    && abs((persisted?[episodeID]?.seconds ?? -1) - saved) < 0.01
                    && persisted?[episodeID]?.finished == false && !library.isFinished(episodeID)
                    && writes.count > beforeWrites && writes.dropFirst(beforeWrites).allSatisfy { $0["id"] as? String == episodeID },
                    ["saved": saved, "player_current": player.current, "card_id": card?.id ?? "none",
                     "write_count": writes.count - beforeWrites])
                samples.append(["id": row.id, "t_start": row.tStart!, "t_end": row.tEnd!,
                    "landed_clock": landed.seconds, "landing_delta_seconds": landed.seconds - row.tStart!,
                    "heard_clock": heard.seconds, "saved": saved])
                persist(done: false)
            }
            check("P-Merge", "catalog_bytes_unchanged_and_baseline_retained",
                try Data(contentsOf: catalogURL) == catalogBefore
                    && hash(catalogBefore) == "02b30ed0aa18aecac9ce00ead967ddc28c7eac31baf8b6d66f1e2adcbcb7966e",
                ["library_sha256": hash(catalogBefore), "baseline_count": records.count])
            check("P-ListenNote", "notes_bytes_unchanged", (try? Data(contentsOf: noteURL)) == notesBefore,
                ["entry_visibility": "host source assertion required", "automatic_note_write": false])
        } catch {
            check("setup", "probe_setup", false, ["error": String(describing: error)])
        }
        persist(done: true)
    }
}
#endif
