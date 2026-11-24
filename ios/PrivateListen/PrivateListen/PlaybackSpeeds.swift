import Foundation

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

struct PlaybackSpeeds {
    private let defaults: UserDefaults
    private let globalKey = "PrivateListen.speed"
    private let episodePrefix = "PrivateListen.episodeSpeed."

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func value(for mediaKey: String?, legacyID: String? = nil) -> ListenSpeed {
        for key in [mediaKey, legacyID].compactMap({ $0 }) {
            if let raw = defaults.string(forKey: episodePrefix + key), let speed = ListenSpeed(rawValue: raw) {
                return speed
            }
        }
        return ListenSpeed.stored(defaults.string(forKey: globalKey))
    }

    func set(_ speed: ListenSpeed, for mediaKey: String?) {
        defaults.set(speed.rawValue, forKey: mediaKey.map { episodePrefix + $0 } ?? globalKey)
    }
}
