import Foundation

/// tip 上没有这两处旧帮手。听后感源文件仍按原调用编译。
/// 事件只丢弃，不写资料库、不入队。桥目录保持空：mesh 回流关闭，库内关联只走本机关键词。

enum UIEventLog {
    static func note(_ name: String, episodeId: String, fields: [String: String] = [:]) {
        _ = (name, episodeId, fields)
    }
}

struct LensBridge: Hashable {
    var id: String
    var fromEpisodeId: String
    var toEpisodeId: String
    var rationaleZh: String
    var strength: Int
    var curated: Bool
    var isPushable: Bool
}

enum LensBridgeCatalog {
    static func loadAll() -> [LensBridge] { [] }
}
