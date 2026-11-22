import Foundation

@main
struct DebugSyntheticEpisodeRouteProbe {
    static func main() throws {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try files.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: directory) }
        let audio = directory.appendingPathComponent("output.mp3")
        // The selector uses the normal local-file check. Decoding is covered by the UI fixture.
        try Data([1]).write(to: audio)
        let fixed = Episode(
            id: "a11ece00150a11ed0000000000261110", title: "Synthetic", guests: [], lensIds: [],
            createdAt: .distantPast, durationSec: 8, audioURL: audio, downloading: false
        )
        var decoy = fixed
        decoy.id = "other-episode"
        let flag = "-OpenSyntheticEpisode"
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [fixed, decoy], arguments: []) == nil)
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [decoy], arguments: [flag, decoy.id]) == nil)
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [fixed], arguments: ["\(flag)=\(fixed.id)"]) == nil)
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [decoy, fixed], arguments: [flag, decoy.id])?.episode == fixed)
        var downloading = fixed
        downloading.downloading = true
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [downloading], arguments: [flag]) == nil)
        try files.removeItem(at: audio)
        precondition(DebugSyntheticEpisodeRoute.resolve(in: [fixed], arguments: [flag]) == nil)
        print("Debug synthetic episode route passed")
    }
}
