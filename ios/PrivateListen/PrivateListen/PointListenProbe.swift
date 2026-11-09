#if DEBUG && targetEnvironment(simulator)
import Foundation
import SwiftUI

/// Explicit opt-in on a disposable simulator with the isolated fixture installed.
/// Uses the real AVPlayer -> onProgress -> LibraryStore -> persisted Continue path.
@MainActor
enum PointListenProbe {
    static var previewEnabled: Bool { ProcessInfo.processInfo.arguments.contains("-PointListenPreview") }

    static func runIfNeeded(player: PlayerModel, library: LibraryStore) async {
        guard ProcessInfo.processInfo.arguments.contains("-PointListenProbe") else { return }
        let reportURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("point-listen-probe.json")
        var checks: [[String: Any]] = []
        func check(_ name: String, _ pass: Bool, _ details: [String: Any] = [:]) {
            checks.append(details.merging(["name": name, "pass": pass]) { _, new in new })
        }
        func wait(_ seconds: Double) async {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        }
        do {
            library.reload()
            guard let episode = library.episodes.first(where: { $0.id == "point-listen-isolated" }) else {
                throw CocoaError(.fileReadNoSuchFile)
            }
            let folder = episode.audioURL.deletingLastPathComponent()
            let packet = try JSONDecoder().decode(ExtractionPacket.self, from: Data(contentsOf: folder.appendingPathComponent("extract.json")))
            let index = try JSONDecoder().decode(PointListenIndex.self, from: Data(contentsOf: folder.appendingPathComponent(PointListening.indexFileName)))
            let audio = try await PointListenAudio.load(episode.audioURL)
            let rows = PointListening.rows(packet: packet, index: index)
            guard rows.count == 5 else { throw CocoaError(.fileReadCorruptFile) }
            let previous = player.onProgress
            var writes = 0
            player.onProgress = { id, seconds, duration in
                writes += 1
                library.remember(id: id, seconds: seconds, duration: duration)
            }
            defer { player.pause(); player.onProgress = previous }
            player.open(episode, resumeAt: 0, force: true)
            await wait(1)
            for row in rows.prefix(2) {
                let before = writes
                let accepted = player.jumpContinue(episode: episode, row: row, index: index, packet: packet, audio: audio)
                check("click_\(row.id)_does_not_write", accepted && before == writes)
                await wait(4.2)
                player.checkpoint()
                let saved = library.resumeSeconds(for: episode.id)
                check("P-Play_\(row.id)_continues_past_t_end",
                    player.isPlaying && player.current > row.tEnd! && player.current >= row.tStart! + 3,
                    ["playhead": player.current, "t_start": row.tStart!, "t_end": row.tEnd!])
                check("P-Continue_\(row.id)_real_saved_head",
                    library.continueEntries(in: [episode]).first?.id == episode.id
                        && abs(saved - player.current) < 0.75 && saved >= row.tStart! + 3
                        && !library.isFinished(episode.id), ["saved": saved])
            }

            _ = player.jumpContinue(episode: episode, row: rows[0], index: index, packet: packet, audio: audio)
            _ = player.jumpContinue(episode: episode, row: rows[2], index: index, packet: packet, audio: audio)
            await wait(3.5)
            check("rapid_taps_latest_seek_wins", player.isPlaying && player.current >= 40 && player.current < 43,
                ["playhead": player.current])
            player.checkpoint()
            let beforeClip = library.resumeSeconds(for: episode.id)
            let beforeClipWrites = writes
            player.playClip(from: 10, to: 10.4, leadIn: 0, minDuration: 0, softCap: 1)
            await wait(2)
            player.checkpoint()
            player.pause()
            player.checkpoint()
            check("clip_window_stop_and_exit_do_not_write", !player.isPlaying && player.isClipPlayback
                && writes == beforeClipWrites && library.resumeSeconds(for: episode.id) == beforeClip)

            _ = player.jumpContinue(episode: episode, row: rows[1], index: index, packet: packet, audio: audio)
            await wait(3.5)
            player.checkpoint()
            check("jump_after_clip_restores_continue_writes", !player.isClipPlayback
                && library.resumeSeconds(for: episode.id) >= rows[1].tStart! + 3 && writes > beforeClipWrites)

            let beforePausedTail = writes
            _ = player.jumpContinue(episode: episode, row: rows[4], index: index, packet: packet, audio: audio)
            player.pause()
            await wait(0.7)
            player.checkpoint()
            check("tail_seek_lands_paused_without_finishing", writes == beforePausedTail && !library.isFinished(episode.id))

            let beforeTail = writes
            let tailAccepted = player.jumpContinue(episode: episode, row: rows[4], index: index, packet: packet, audio: audio)
            check("tail_click_does_not_finish", tailAccepted && writes == beforeTail && !library.isFinished(episode.id))
            await wait(2)
            player.checkpoint()
            check("tail_real_play_keeps_original_finished_rule", library.isFinished(episode.id))

            var wrongIndex = index
            wrongIndex.audioSHA256 = String(repeating: "0", count: 64)
            let oldHead = player.current
            let refused = !player.jumpContinue(episode: episode, row: rows[0], index: wrongIndex, packet: packet, audio: audio)
            check("PL-D2_player_refuses_mismatched_binding", refused && player.current == oldHead)

            _ = player.jumpContinue(episode: episode, row: rows[2], index: index, packet: packet, audio: audio)
            var other = episode
            other.id = "point-listen-other-isolated"
            player.open(other, resumeAt: 5, force: true)
            await wait(1.5)
            check("cross_episode_cancels_old_seek", player.currentID == other.id && player.current >= 5 && player.current < 9)
        } catch {
            check("probe_setup", false, ["error": String(describing: error)])
        }
        let report: [String: Any] = ["schema": "shengjian.point-listen.probe.v1", "device": "simulator",
            "done": true, "pass": !checks.isEmpty && checks.allSatisfy { $0["pass"] as? Bool == true }, "checks": checks]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: reportURL, options: .atomic)
        }
    }
}

/// Review the production list component with an explicitly selected isolated sample.
struct PointListenPreview: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var extracts: ExtractStore

    var body: some View {
        NavigationStack {
            ScrollView {
                if let episode = library.episodes.first(where: { $0.id == "point-listen-isolated" }) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("隔离样包 · 测试音").font(.caption).foregroundStyle(.secondary)
                        ExtractDetailSection(episode: episode, extracts: extracts)
                    }
                    .padding(24)
                    .onAppear { extracts.load(episode: episode) }
                } else {
                    Text("隔离样包未安装")
                }
            }
            .navigationTitle("声间")
        }
    }
}
#endif
