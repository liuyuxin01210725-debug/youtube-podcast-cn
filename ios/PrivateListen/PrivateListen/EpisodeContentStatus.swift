import Foundation

enum HumanReviewState: Equatable {
    case pending
}

enum EpisodeContentKind: Equatable {
    case chineseMaster
    case pendingSample
    case pendingSampleInvalid
    case unverifiedAudio
    case downloading
    case unavailableAudio
    case unavailableRevision
}

struct EpisodeContentStatus: Equatable {
    let kind: EpisodeContentKind
    let humanReview: HumanReviewState
    let primaryLabel: String
    let reviewLabel: String
    let detailLabel: String
    let listBadgeLabel: String?

    var playerSummaryLabel: String {
        "\(primaryLabel) · \(detailLabel)"
    }

    static func resolve(episode: Episode) -> EpisodeContentStatus {
        let review: HumanReviewState = .pending
        let reviewText = "未人审"

        if episode.pendingTranscript == .invalid {
            return EpisodeContentStatus(
                kind: .pendingSampleInvalid,
                humanReview: review,
                primaryLabel: "待核听样（校验失败）",
                reviewLabel: reviewText,
                detailLabel: "自动识别 · 校验失败 · \(reviewText)",
                listBadgeLabel: "待核听样·校验失败"
            )
        }
        if episode.unavailableReason == .revision {
            return EpisodeContentStatus(
                kind: .unavailableRevision,
                humanReview: review,
                primaryLabel: "版本待核",
                reviewLabel: reviewText,
                detailLabel: "版本校验未通过 · 暂不可播 · \(reviewText)",
                listBadgeLabel: "版本待核"
            )
        }
        if episode.downloading {
            return EpisodeContentStatus(
                kind: .downloading,
                humanReview: review,
                primaryLabel: "下载中",
                reviewLabel: reviewText,
                detailLabel: "正在从 iCloud 下载 · \(reviewText)",
                listBadgeLabel: nil
            )
        }
        if episode.unavailableReason == .audio || !episode.isPlayableNow {
            return EpisodeContentStatus(
                kind: .unavailableAudio,
                humanReview: review,
                primaryLabel: "音轨不可用",
                reviewLabel: reviewText,
                detailLabel: "暂无可用音频 · \(reviewText)",
                listBadgeLabel: "音轨不可用"
            )
        }
        if episode.pendingTranscript != nil {
            return EpisodeContentStatus(
                kind: .pendingSample,
                humanReview: review,
                primaryLabel: "待核听样",
                reviewLabel: reviewText,
                detailLabel: "中文原音 · 自动识别 · \(reviewText)",
                listBadgeLabel: "待核听样"
            )
        }
        let hasCompleteRevision = episode.mediaRevision != nil
            && (episode.revisionFiles?.isSuperset(of: MediaRevision.requiredFiles) == true)
        let hasLegacyRenderSidecar = episode.mediaRevision == nil && episode.hasChineseRenderSidecar
        if episode.audioKind == "tts" || hasCompleteRevision || hasLegacyRenderSidecar {
            return EpisodeContentStatus(
                kind: .chineseMaster,
                humanReview: review,
                primaryLabel: "中文成片",
                reviewLabel: reviewText,
                detailLabel: reviewText,
                listBadgeLabel: nil
            )
        }
        return EpisodeContentStatus(
            kind: .unverifiedAudio,
            humanReview: review,
            primaryLabel: "来源待核",
            reviewLabel: reviewText,
            detailLabel: "有音频 · 缺成片信息 · \(reviewText)",
            listBadgeLabel: "来源待核"
        )
    }
}
