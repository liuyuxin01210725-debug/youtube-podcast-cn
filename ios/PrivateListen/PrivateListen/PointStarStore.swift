import Foundation

/// 要点级收藏（切片 B）：与整集 `starred`、续听 `ListenMark` 分存。
/// 点星即收藏；`reason` 四态可后置，本切片不强制选理由。
struct PointStar: Codable, Hashable, Identifiable {
    var workId: String
    var pointId: String
    var kind: String
    var reason: String?
    var starredAt: String

    var id: String { "\(workId)|\(pointId)" }

    enum CodingKeys: String, CodingKey {
        case workId = "work_id"
        case pointId = "point_id"
        case kind, reason
        case starredAt = "starred_at"
    }
}

@MainActor
final class PointStarStore: ObservableObject {
    @Published private(set) var stars: [String: PointStar] = [:]

    private let storageKey = "PrivateListen.pointStars"

    init() {
        load()
    }

    func isStarred(workId: String, pointId: String) -> Bool {
        stars[Self.key(workId: workId, pointId: pointId)] != nil
    }

    func star(for workId: String, pointId: String) -> PointStar? {
        stars[Self.key(workId: workId, pointId: pointId)]
    }

    /// 点星即收藏；取消要点星不撤销整集深抽、不入队人/节目。
    func toggle(workId: String, pointId: String, kind: String, reason: String? = nil) {
        let k = Self.key(workId: workId, pointId: pointId)
        if stars[k] != nil {
            stars.removeValue(forKey: k)
        } else {
            stars[k] = PointStar(
                workId: workId,
                pointId: pointId,
                kind: kind,
                reason: reason,
                starredAt: ISO8601DateFormatter().string(from: Date())
            )
        }
        persist()
    }

    func setReason(workId: String, pointId: String, reason: String?) {
        let k = Self.key(workId: workId, pointId: pointId)
        guard var existing = stars[k] else { return }
        existing.reason = reason
        stars[k] = existing
        persist()
    }

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let list = try? JSONDecoder().decode([PointStar].self, from: data) else { return }
        stars = Dictionary(uniqueKeysWithValues: list.map { ($0.id, $0) })
    }

    private func persist() {
        let list = stars.values.sorted { $0.starredAt < $1.starredAt }
        if let data = try? JSONEncoder().encode(list) {
            UserDefaults.standard.set(data, forKey: storageKey)
        }
    }

    nonisolated static func key(workId: String, pointId: String) -> String {
        "\(workId)|\(pointId)"
    }
}
