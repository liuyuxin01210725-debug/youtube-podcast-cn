import Foundation

@main
struct PointListenChecks {
    static func main() async throws {
        // Boundary first: seek-only never reaches the original near-end reducer.
        for target in [584.9, 585.0, 590.0] {
            var gate = JumpContinueProgress()
            gate.didLand(at: target)
            precondition(!gate.allowsCheckpoint)
            precondition(!gate.observe(seconds: target, isPlaying: true))
            precondition(!gate.observe(seconds: target + 0.5, isPlaying: false))
            precondition(gate.observe(seconds: target + 0.05, isPlaying: true))
            let marks = ContinueListening.record(marks: [:], id: "fixture", seconds: target + 0.05,
                duration: 600, touchedAt: 1, clipSuppressListenMark: false)
            precondition(marks["fixture"]?.finished == (target >= 585))
        }
        print("ok  seek-only vs actual playback: 15-second finished boundary unchanged")

        let root = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "fixtures/point-listen/episodes/point-listen-isolated")
        let packet = try JSONDecoder().decode(ExtractionPacket.self, from: Data(contentsOf: root.appendingPathComponent("extract.json")))
        let index = try JSONDecoder().decode(PointListenIndex.self, from: Data(contentsOf: root.appendingPathComponent("point-listen.json")))
        let audio = try await PointListenAudio.load(root.appendingPathComponent("output.mp3"))
        let rows = PointListening.rows(packet: packet, index: index)
        precondition(rows.count == 5 && rows.map(\.id) == ["v1", "v2", "v3", "v4", "v5"])
        precondition(rows[1].viewpointText.contains("？") && rows[0].span != nil)
        func refused(_ row: ListenViewpoint, _ changedIndex: PointListenIndex? = nil,
                     _ changedPacket: ExtractionPacket? = nil, hash: String? = nil) -> Bool {
            PointListening.refusal(row: row, index: changedIndex ?? index, packet: changedPacket ?? packet,
                episodeID: packet.workId, audioHash: hash ?? audio.sha256, duration: audio.duration) != nil
        }
        precondition(rows.allSatisfy { !refused($0) })
        precondition(audio.sha256 == index.audioSHA256 && audio.isCurrent && abs(audio.duration - 90) < 1)
        print("ok  bound isolated output.mp3 decoded; semantic order and question text; span stays String?")

        var missing = rows[0]; missing.tStart = nil
        var changed = index; changed.viewpoints[0] = missing
        precondition(refused(missing, changed))
        precondition(PointListening.refusal(row: rows[0], index: nil, packet: packet, episodeID: packet.workId,
            audioHash: audio.sha256, duration: audio.duration) != nil)
        precondition(!refused(rows[1], changed)) // segment degradation does not disable its sibling
        print("ok  PL-D1 missing anchor/index denied; valid sibling remains enabled")

        for hash in ["", String(repeating: "0", count: 64), "73fd605a9fe7b081cc425f46dd515c93f90a2a22346159c4bbcc5bbe72c87ae0"] {
            changed = index; changed.audioSHA256 = hash
            precondition(refused(rows[0], changed))
        }
        changed = index; changed.workId = "42786039aaf346a1bf851613199e260a"
        precondition(refused(rows[0], changed))
        changed = index; changed.packetId = "another-packet"
        precondition(refused(rows[0], changed))
        print("ok  PL-D2 missing/mismatched audio and wrong episode/packet denied; document hash is not audio hash")

        changed = index; changed.transcriptHash = "deadbeefdeadbeef"
        precondition(refused(rows[0], changed))
        var stale = packet; stale.status = .stale
        precondition(refused(rows[0], index, stale))
        stale = packet; stale.packetId += "-deep-mock"
        changed = index; changed.packetId = stale.packetId
        precondition(refused(rows[0], changed, stale))
        print("ok  PL-D3 transcript stale/status stale/mock packet denied")

        for value in [-1.0, Double.nan, Double.infinity] {
            var invalid = rows[0]; invalid.tStart = value
            changed = index; changed.viewpoints[0] = invalid
            precondition(refused(invalid, changed))
        }
        var invalid = rows[0]; invalid.tEnd = invalid.tStart
        changed = index; changed.viewpoints[0] = invalid
        precondition(refused(invalid, changed))
        changed = index; changed.timeline = "source.start_ms"
        precondition(refused(rows[0], changed))
        print("ok  PL-D4 illegal numeric/interval/source axis denied")

        invalid = rows[0]; invalid.tStart = audio.duration; invalid.tEnd = nil
        changed = index; changed.viewpoints[0] = invalid
        precondition(refused(invalid, changed))
        invalid = rows[0]; invalid.tEnd = audio.duration + 1
        changed = index; changed.viewpoints[0] = invalid
        precondition(refused(invalid, changed))
        print("ok  PL-D5 out-of-duration start/end denied")

        let legacyRows = PointListening.rows(packet: packet, index: nil)
        precondition(legacyRows.count == 5 && legacyRows[0].sourcePointId == packet.claims[0].id)
        var sharedIDs = packet
        sharedIDs.openQuestions = [ExtractOpenQuestion(id: packet.claims[0].id, text: "同名旧槽能否保留独立身份？", status: .candidate)]
        let unique = PointListening.rows(packet: sharedIDs, index: nil)
        precondition(Set(unique.map(\.id)).count == unique.count)
        let marks = ContinueListening.afterLeavingPlayer(marks: [:], dismissed: [], durations: [packet.workId: audio.duration],
            episodeID: packet.workId, seconds: rows[1].tStart! + 4.2, duration: audio.duration,
            touchedAt: 100, clipSuppressListenMark: false)
        precondition(marks.cardID == packet.workId && marks.marks[packet.workId]?.finished == false)
        print("ok  P-Continue synthetic 4.2s after fractional-second jump; legacy reading preserves point IDs")
        print("point listen checks passed")
    }
}
