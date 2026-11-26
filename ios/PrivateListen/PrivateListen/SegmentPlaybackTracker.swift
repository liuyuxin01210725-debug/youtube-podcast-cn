import Foundation

enum SegmentPlaybackTracker {
    static func activeNodeID(
        at seconds: Double,
        in nodes: [ListenViewpoint],
        duration: Double,
        isCurrentEpisode: Bool,
        isAudioCurrent: Bool,
        isPendingTranscript: Bool,
        isUnavailable: Bool
    ) -> String? {
        guard isCurrentEpisode, isAudioCurrent, !isPendingTranscript, !isUnavailable else {
            return nil
        }
        return activeNodeID(at: seconds, in: nodes, duration: duration)
    }

    static func activeNodeID(
        at seconds: Double,
        in nodes: [ListenViewpoint],
        duration: Double
    ) -> String? {
        guard seconds.isFinite, seconds >= 0,
              duration.isFinite, duration > 0,
              !nodes.isEmpty else {
            return nil
        }

        var validNodes: [(index: Int, node: ListenViewpoint, start: Double, end: Double?)] = []
        for (index, node) in nodes.enumerated() {
            guard let start = node.tStart,
                  start.isFinite, start >= 0, start < duration else {
                continue
            }
            if let end = node.tEnd {
                guard end.isFinite, end > start, end <= duration else {
                    continue
                }
                validNodes.append((index, node, start, end))
            } else {
                validNodes.append((index, node, start, nil))
            }
        }

        let started = validNodes.filter { $0.start <= seconds }
        guard !started.isEmpty else { return nil }

        let covering = started.filter { item in
            guard let end = item.end else { return false }
            return seconds < end
        }
        if let exact = covering.max(by: { lhs, rhs in
            lhs.start == rhs.start ? lhs.index < rhs.index : lhs.start < rhs.start
        }) {
            return exact.node.id
        }

        return started.max(by: { lhs, rhs in
            lhs.start == rhs.start ? lhs.index < rhs.index : lhs.start < rhs.start
        })?.node.id
    }
}

struct SegmentScrollFollowState: Equatable {
    var isFollowing: Bool = true
    var visibleNodeIDs: Set<String> = []
    var lastAutoScrolledID: String? = nil

    mutating func userDidScroll() {
        isFollowing = false
    }

    mutating func nodeAppeared(_ id: String) {
        visibleNodeIDs.insert(id)
    }

    mutating func nodeDisappeared(_ id: String) {
        visibleNodeIDs.remove(id)
    }

    mutating func returnToCurrent() {
        isFollowing = true
        lastAutoScrolledID = nil
    }

    mutating func markAutoScrolled(_ id: String?) {
        lastAutoScrolledID = id
    }

    mutating func resetForNewEpisode() {
        isFollowing = true
        visibleNodeIDs.removeAll()
        lastAutoScrolledID = nil
    }

    func shouldAutoScroll(to activeID: String?) -> Bool {
        guard isFollowing, let activeID else { return false }
        return activeID != lastAutoScrolledID
    }

    func showsBackToCurrentButton(activeID: String?) -> Bool {
        guard let activeID else { return false }
        return !isFollowing || !visibleNodeIDs.contains(activeID)
    }
}
