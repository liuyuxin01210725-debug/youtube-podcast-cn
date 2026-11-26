import Foundation

struct ListenPick: Equatable, Identifiable {
    let id: String
    let node: ListenViewpoint
    let tStart: Double
    let tEnd: Double
    let durationSec: Double
    let anchorSpanLabel: String
    let reason: String
}

enum ListenPicks {
    static let minCount = 2
    static let maxCount = 3
    static let minDurationSec = 1.0

    private enum SlotMatch {
        case none
        case invalidOrDiscarded
        case claim(ExtractClaim)
        case method(ExtractMethod)
        case quote(ExtractQuote)
        case openQuestion(ExtractOpenQuestion)
    }

    private struct Candidate {
        let pick: ListenPick
        let isConfirmed: Bool
        let isHighDistinction: Bool
        let hasValidSpan: Bool
        let confidence: Double
    }

    static func anchorSpanLabel(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "依据片段约 1 秒" }
        let total = max(1, Int(seconds.rounded()))
        if total < 60 {
            return "依据片段约 \(total) 秒"
        }
        let minutes = total / 60
        let secs = total % 60
        if secs == 0 {
            return "依据片段约 \(minutes) 分"
        }
        return "依据片段约 \(minutes) 分 \(secs) 秒"
    }

    static func select(
        nodes: [ListenViewpoint],
        index: PointListenIndex?,
        packet: ExtractionPacket?,
        episode: Episode,
        audioHash: String?,
        duration: Double
    ) -> [ListenPick] {
        guard episode.unavailableReason == nil,
              episode.pendingTranscript == nil,
              episode.isPlayableNow,
              let index,
              let packet,
              let audioHash,
              duration.isFinite, duration > 0 else {
            return []
        }

        var candidates: [Candidate] = []
        for row in nodes {
            guard PointListening.refusal(
                row: row,
                index: index,
                packet: packet,
                episodeID: episode.id,
                audioHash: audioHash,
                duration: duration
            ) == nil else {
                continue
            }
            guard let tStart = row.tStart,
                  let tEnd = row.tEnd,
                  tStart.isFinite, tEnd.isFinite,
                  tEnd > tStart, tEnd <= duration else {
                continue
            }
            let spanSec = tEnd - tStart
            guard spanSec >= minDurationSec else { continue }

            let viewpointTrimmedLen = trimmedLength(row.viewpointText)
            guard viewpointTrimmedLen >= 8 else { continue }

            let hasValidSpan = trimmedLength(row.span) >= 8
            let match = uniqueSlotMatch(for: row, in: packet)
            switch match {
            case .invalidOrDiscarded:
                continue
            case .none:
                guard hasValidSpan, viewpointTrimmedLen >= 12 else { continue }
            case .claim, .method, .quote, .openQuestion:
                break
            }

            let primaryReason = primaryReasonLabel(for: match)
            let evidenceSuffix = hasValidSpan ? " · 有译稿依据" : " · 已校验成片段"
            let pick = ListenPick(
                id: row.id,
                node: row,
                tStart: tStart,
                tEnd: tEnd,
                durationSec: spanSec,
                anchorSpanLabel: anchorSpanLabel(spanSec),
                reason: primaryReason + evidenceSuffix
            )

            candidates.append(
                Candidate(
                    pick: pick,
                    isConfirmed: isConfirmedMatch(match),
                    isHighDistinction: isHighDistinctionMatch(match),
                    hasValidSpan: hasValidSpan,
                    confidence: matchConfidence(match) ?? 0.0
                )
            )
        }

        guard candidates.count >= minCount else { return [] }
        if candidates.count <= maxCount {
            return candidates.map(\.pick).sorted {
                $0.tStart == $1.tStart ? $0.id < $1.id : $0.tStart < $1.tStart
            }
        }

        let prioritized = candidates.sorted(by: higherPriority)
        let minGap = max(15.0, min(90.0, duration * 0.12))
        var chosen: [Candidate] = []

        func containsID(_ id: String) -> Bool {
            chosen.contains { $0.pick.id == id }
        }

        func nonOverlapping(_ cand: Candidate) -> Bool {
            chosen.allSatisfy { picked in
                cand.pick.tEnd <= picked.pick.tStart || cand.pick.tStart >= picked.pick.tEnd
            }
        }

        func satisfiesGap(_ cand: Candidate) -> Bool {
            chosen.allSatisfy { picked in
                abs(cand.pick.tStart - picked.pick.tStart) >= minGap
            }
        }

        if let first = prioritized.first {
            chosen.append(first)
        }

        while chosen.count < maxCount {
            if let next = prioritized.first(where: { !containsID($0.pick.id) && nonOverlapping($0) && satisfiesGap($0) }) {
                chosen.append(next)
            } else if let next = prioritized.first(where: { !containsID($0.pick.id) && nonOverlapping($0) }) {
                chosen.append(next)
            } else if let next = prioritized.first(where: { !containsID($0.pick.id) }) {
                chosen.append(next)
            } else {
                break
            }
        }

        return chosen.map(\.pick).sorted {
            $0.tStart == $1.tStart ? $0.id < $1.id : $0.tStart < $1.tStart
        }
    }

    private static func trimmedLength(_ text: String?) -> Int {
        guard let text else { return 0 }
        return text.components(separatedBy: .whitespacesAndNewlines).joined().count
    }

    private static func uniqueSlotMatch(for row: ListenViewpoint, in packet: ExtractionPacket) -> SlotMatch {
        let targetID: String
        if let sourcePointId = row.sourcePointId?.trimmingCharacters(in: .whitespacesAndNewlines),
           !sourcePointId.isEmpty {
            targetID = sourcePointId
        } else {
            let fallbackID = row.id.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !fallbackID.isEmpty else { return .none }
            targetID = fallbackID
        }

        let kind = row.sourceKind?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        var matches: [SlotMatch] = []

        func appendMatchingClaims() {
            for item in packet.claims where item.id == targetID {
                matches.append(.claim(item))
            }
        }
        func appendMatchingMethods() {
            for item in packet.methods where item.id == targetID {
                matches.append(.method(item))
            }
        }
        func appendMatchingQuotes() {
            for item in packet.quotes where item.id == targetID {
                matches.append(.quote(item))
            }
        }
        func appendMatchingQuestions() {
            for item in packet.openQuestions where item.id == targetID {
                matches.append(.openQuestion(item))
            }
        }

        switch kind {
        case "":
            appendMatchingClaims()
            appendMatchingMethods()
            appendMatchingQuotes()
            appendMatchingQuestions()
        case "claim":
            appendMatchingClaims()
        case "method":
            appendMatchingMethods()
        case "quote":
            appendMatchingQuotes()
        case "open", "open_question", "question":
            appendMatchingQuestions()
        default:
            return .none
        }

        guard matches.count == 1, let matched = matches.first else {
            return .none
        }

        switch matched {
        case .claim(let item):
            if item.status == .discarded || item.lowConfidence == true { return .invalidOrDiscarded }
        case .method(let item):
            if item.status == .discarded || item.lowConfidence == true { return .invalidOrDiscarded }
        case .quote(let item):
            if item.status == .discarded || item.lowConfidence == true { return .invalidOrDiscarded }
        case .openQuestion(let item):
            if item.status == .discarded || item.lowConfidence == true { return .invalidOrDiscarded }
        case .none, .invalidOrDiscarded:
            break
        }
        return matched
    }

    private static func primaryReasonLabel(for match: SlotMatch) -> String {
        switch match {
        case .none, .invalidOrDiscarded:
            return "核心观点"
        case .claim(let item):
            if item.status == .confirmed { return "已确认观点" }
            if item.pointType == "claim_controversy" || item.stance == "争议" { return "争议观点" }
            return "核心观点"
        case .method(let item):
            return item.status == .confirmed ? "已确认观点" : "实操方法"
        case .openQuestion(let item):
            return item.status == .confirmed ? "已确认观点" : "关键问题"
        case .quote(let item):
            return item.status == .confirmed ? "已确认观点" : "核心原话"
        }
    }

    private static func isConfirmedMatch(_ match: SlotMatch) -> Bool {
        switch match {
        case .claim(let item): return item.status == .confirmed
        case .method(let item): return item.status == .confirmed
        case .quote(let item): return item.status == .confirmed
        case .openQuestion(let item): return item.status == .confirmed
        case .none, .invalidOrDiscarded: return false
        }
    }

    private static func isHighDistinctionMatch(_ match: SlotMatch) -> Bool {
        switch match {
        case .claim(let item):
            return item.pointType == "claim_controversy"
                || item.stance == "争议"
                || item.pointType == "claim_debate"
                || item.pointType == "claim_outlook"
        case .method, .openQuestion:
            return true
        case .quote, .none, .invalidOrDiscarded:
            return false
        }
    }

    private static func matchConfidence(_ match: SlotMatch) -> Double? {
        switch match {
        case .claim(let item): return item.confidence
        case .method(let item): return item.confidence
        case .quote(let item): return item.confidence
        case .openQuestion(let item): return item.confidence
        case .none, .invalidOrDiscarded: return nil
        }
    }

    private static func higherPriority(_ lhs: Candidate, _ rhs: Candidate) -> Bool {
        if lhs.isConfirmed != rhs.isConfirmed { return lhs.isConfirmed }
        if lhs.isHighDistinction != rhs.isHighDistinction { return lhs.isHighDistinction }
        if lhs.hasValidSpan != rhs.hasValidSpan { return lhs.hasValidSpan }
        if lhs.confidence != rhs.confidence { return lhs.confidence > rhs.confidence }
        if lhs.pick.tStart != rhs.pick.tStart { return lhs.pick.tStart < rhs.pick.tStart }
        return lhs.pick.id < rhs.pick.id
    }
}
