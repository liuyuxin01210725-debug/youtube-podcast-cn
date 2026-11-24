import Foundation

enum ListenAlongLink: Equatable, Identifiable {
    case point(id: String, text: String, tStart: Double, reason: String)
    case listenNote(episodeId: String, summary: String, reason: String)

    var id: String {
        switch self {
        case let .point(id, _, _, _):
            return "point.\(id)"
        case let .listenNote(episodeId, _, _):
            return "listenNote.\(episodeId)"
        }
    }
}

/// 最小可解释关联：仅按时间邻近（≤30s）、同集听后感与可选字面关键词重合匹配。
enum ListenAlongAssociation {
    static let maxPointDistanceSeconds: Double = 30
    static let maxPointLinks = 2
    static let maxTotalLinks = 3

    static func links(
        for note: ListenAlongNote,
        points: [ListenViewpoint],
        listenNote: ListenNote?
    ) -> [ListenAlongLink] {
        var result: [ListenAlongLink] = []

        let pointCandidates: [(point: ListenViewpoint, tStart: Double, distance: Double, keywords: [String])] =
            points.compactMap { point in
                guard let start = point.tStart, start.isFinite, start >= 0 else { return nil }
                let end: Double
                if let tEnd = point.tEnd, tEnd.isFinite, tEnd >= start {
                    end = tEnd
                } else {
                    end = start
                }
                let distance = intervalDistance(note.positionSec, start: start, end: end)
                guard distance <= maxPointDistanceSeconds else { return nil }
                let keywords = sharedKeywords(between: note.text, and: point.viewpointText)
                return (point, start, distance, keywords)
            }
            .sorted { lhs, rhs in
                if lhs.distance != rhs.distance { return lhs.distance < rhs.distance }
                if lhs.keywords.count != rhs.keywords.count { return lhs.keywords.count > rhs.keywords.count }
                if lhs.tStart != rhs.tStart { return lhs.tStart < rhs.tStart }
                return lhs.point.id < rhs.point.id
            }

        for candidate in pointCandidates.prefix(maxPointLinks) {
            var reason = "时间接近 \(listenClock(candidate.tStart))"
            if !candidate.keywords.isEmpty {
                let matched = candidate.keywords.prefix(2).joined(separator: "、")
                reason += " · 关键词重合：\(matched)"
            }
            result.append(
                .point(
                    id: candidate.point.id,
                    text: candidate.point.viewpointText,
                    tStart: candidate.tStart,
                    reason: reason
                )
            )
        }

        if let listenNote, listenNote.episodeId == note.episodeId, listenNote.hasContent {
            let noteHaystack = [
                listenNote.headline,
                listenNote.body,
                listenNote.tags.joined(separator: " "),
            ].joined(separator: "\n")
            let keywords = sharedKeywords(between: note.text, and: noteHaystack)
            let reason: String
            if !keywords.isEmpty {
                let matched = keywords.prefix(2).joined(separator: "、")
                reason = "同一期的听后感 · 关键词重合：\(matched)"
            } else {
                reason = "同一期的听后感"
            }
            let summary = listenNote.listSummary ?? "本期听后感"
            result.append(
                .listenNote(
                    episodeId: listenNote.episodeId,
                    summary: summary,
                    reason: reason
                )
            )
        }

        return Array(result.prefix(maxTotalLinks))
    }

    private static func intervalDistance(_ position: Double, start: Double, end: Double) -> Double {
        let clamped = position.isFinite ? max(0, position) : 0
        if clamped < start { return start - clamped }
        if clamped > end { return clamped - end }
        return 0
    }

    static func sharedKeywords(between lhs: String, and rhs: String) -> [String] {
        let left = lhs.trimmingCharacters(in: .whitespacesAndNewlines)
        let right = rhs.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !left.isEmpty, !right.isEmpty else { return [] }

        var matched: [String] = []
        var seen = Set<String>()

        func appendIfNew(_ token: String) {
            let key = token.lowercased()
            if seen.insert(key).inserted {
                matched.append(token)
            }
        }

        for kw in ListenNoteDownstream.extractKeywords(from: left) where right.localizedCaseInsensitiveContains(kw) {
            appendIfNew(kw)
        }

        for token in asciiTokens(in: left) where right.localizedCaseInsensitiveContains(token) {
            appendIfNew(token)
        }

        return matched
    }

    private static func asciiTokens(in text: String) -> [String] {
        text.split { !$0.isASCII || (!$0.isLetter && !$0.isNumber) }
            .map(String.init)
            .filter { $0.count >= 3 }
    }
}
