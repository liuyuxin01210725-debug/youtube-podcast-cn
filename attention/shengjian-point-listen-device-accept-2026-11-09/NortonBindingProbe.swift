import Foundation

/// Read-only host probe using the production decoder, audio verifier and refusal gate.
/// Running on device-pulled bytes does not establish UI taps or audible playback.
@main
struct NortonBindingProbe {
    static func main() async throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let packet = ExtractPacketDecoding.decode(try Data(contentsOf: root.appendingPathComponent("extract.json")))!
        let index = try JSONDecoder().decode(PointListenIndex.self, from: Data(contentsOf: root.appendingPathComponent("point-listen.json")))
        let audio = try await PointListenAudio.load(root.appendingPathComponent("output.mp3"))
        let rows = PointListening.rows(packet: packet, index: index)
        precondition(packet.workId == "42786039aaf346a1bf851613199e260a")
        precondition(packet.transcriptHash == "16962cd5c25558ff" && index.transcriptHash == packet.transcriptHash)
        precondition(audio.sha256 == "fcb337634866181e6107eb6cb519006fe9102c756b35f83bb8d1410a04749f25")
        // MP3 ffprobe container duration=3809.112; AVFoundation reports 3809.160 (48 ms).
        precondition(abs(audio.duration - 3809.112) < 0.1 && audio.isCurrent,
            "AVFoundation duration=\(audio.duration), unchanged=\(audio.isCurrent)")
        precondition(rows.map(\.sourcePointId) == ["s1", "s2", "s3", "i3", "i8", "i9", "i11"])
        let checks = rows.map { row -> [String: Any] in
            let source = packet.claims.first { $0.id == row.sourcePointId }!
            precondition(row.span == source.span && row.tStart == source.tStart && row.tEnd == source.tEnd)
            precondition(row.tEnd! > row.tStart! && row.tEnd! <= audio.duration)
            let refusal = PointListening.refusal(row: row, index: index, packet: packet,
                episodeID: packet.workId, audioHash: audio.sha256, duration: audio.duration)
            precondition(refusal == nil, "Norton row refused: \(row.id): \(refusal ?? "")")
            return ["id": row.id, "pass": true, "t_start": row.tStart!, "t_end": row.tEnd!]
        }
        let report: [String: Any] = ["schema": "shengjian.point-listen.binding-probe.v1",
            "execution_platform": "macOS", "input_directory": root.path, "pass": true,
            "packet_id": packet.packetId, "transcript_hash": index.transcriptHash,
            "audio_sha256": audio.sha256, "duration_seconds": audio.duration,
            "ffprobe_duration_seconds": 3809.112, "duration_difference_seconds": audio.duration - 3809.112,
            "checks": checks, "ui_taps_performed": false, "semantic_landing_verified": false]
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
