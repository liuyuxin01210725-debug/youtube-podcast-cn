#if DEBUG
import Foundation

struct DebugSyntheticEpisodeRoute: Hashable {
    private static let flag = "-OpenSyntheticEpisode"
    private static let episodeID = "a11ece00150a11ed0000000000261110"

    let episode: Episode

    private init(episode: Episode) {
        self.episode = episode
    }

    static func requested(arguments: [String] = ProcessInfo.processInfo.arguments) -> Bool {
        arguments.contains(flag)
    }

    static func resolve(
        in episodes: [Episode],
        arguments: [String] = ProcessInfo.processInfo.arguments
    ) -> Self? {
        guard requested(arguments: arguments),
              let episode = episodes.first(where: { $0.id == episodeID }),
              FileManager.default.fileExists(atPath: episode.audioURL.path),
              episode.isPlayableNow else { return nil }
        return Self(episode: episode)
    }
}
#endif
