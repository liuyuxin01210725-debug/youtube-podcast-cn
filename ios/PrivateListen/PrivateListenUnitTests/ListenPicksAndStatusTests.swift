import CryptoKit
import Foundation
import XCTest
@testable import PrivateListen

final class ListenPicksAndStatusTests: XCTestCase {
    private var tempDirs: [URL] = []

    override func tearDown() {
        for dir in tempDirs {
            try? FileManager.default.removeItem(at: dir)
        }
        tempDirs.removeAll()
        super.tearDown()
    }

    private func makeTempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        tempDirs.append(dir)
        return dir
    }

    private func makeAudioEpisode(
        id: String = "ep-180",
        duration: Double = 180,
        audioKind: String? = "tts",
        mediaRevision: String? = nil,
        revisionFiles: Set<String>? = nil,
        pending: PendingTranscriptReference? = nil,
        unavailable: Episode.UnavailableReason? = nil,
        hasChineseRenderSidecar: Bool = true
    ) throws -> (Episode, URL, String) {
        let dir = try makeTempDir()
        let audioURL = dir.appendingPathComponent("output.mp3")
        let bytes = Data("synthetic-audio-payload-\(id)".utf8)
        try bytes.write(to: audioURL)
        let sha = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let ep = Episode(
            id: id,
            title: "测试节目",
            show: "测试",
            guests: [],
            lensIds: [],
            createdAt: Date(timeIntervalSince1970: 1_764_000_000),
            durationSec: duration,
            audioURL: audioURL,
            downloading: false,
            artworkFile: nil,
            artworkKind: nil,
            pendingTranscript: pending,
            mediaRevision: mediaRevision,
            revisionFiles: revisionFiles,
            unavailableReason: unavailable,
            audioKind: audioKind,
            hasChineseRenderSidecar: hasChineseRenderSidecar
        )
        return (ep, dir, sha)
    }

    private func makePacket(
        workId: String = "ep-180",
        packetId: String = "pkt-1",
        transcriptHash: String = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        mode: ExtractMode = .deep,
        status: PacketStatus = .ready,
        claims: [ExtractClaim] = [],
        methods: [ExtractMethod] = [],
        quotes: [ExtractQuote] = [],
        openQuestions: [ExtractOpenQuestion] = []
    ) -> ExtractionPacket {
        ExtractionPacket(
            packetId: packetId,
            workId: workId,
            schemaId: .general,
            mode: mode,
            status: status,
            createdAt: "2026-11-26T00:00:00Z",
            summaryLine: "摘要",
            transcriptHash: transcriptHash,
            costUnits: nil,
            failureReason: nil,
            degradedFrom: nil,
            claims: claims,
            methods: methods,
            entities: [],
            quotes: quotes,
            openQuestions: openQuestions,
            roster: nil,
            actions: nil,
            secondarySchemas: nil,
            pendingConfirmCount: nil
        )
    }

    private func makeFivePicksFixture(audioHash: String) -> (PointListenIndex, ExtractionPacket) {
        let claims = [
            ExtractClaim(id: "claim-A", text: "A", stance: nil, span: "这是足够长度的译稿原文依据片段A", tStart: 10, tEnd: 22, confidence: 0.85, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
            ExtractClaim(id: "claim-B", text: "B", stance: nil, span: "这是足够长度的译稿原文依据片段B", tStart: 18, tEnd: 30, confidence: 0.85, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
            ExtractClaim(id: "claim-C", text: "C", stance: nil, span: "这是足够长度的译稿原文依据片段C", tStart: 40, tEnd: 55, confidence: 0.85, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
            ExtractClaim(id: "claim-D", text: "D", stance: nil, span: "这是足够长度的译稿原文依据片段D", tStart: 95, tEnd: 110, confidence: 0.85, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
            ExtractClaim(id: "claim-E", text: "E", stance: nil, span: "这是足够长度的译稿原文依据片段E", tStart: 130, tEnd: 140, confidence: 0.85, status: .confirmed, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
        ]
        let viewpoints = [
            ListenViewpoint(id: "A", viewpointText: "合成观点 A 超过八个汉字的完整标题", span: "这是足够长度的译稿原文依据片段A", tStart: 10, tEnd: 22, indexInclude: true, sourceKind: "claim", sourcePointId: "claim-A"),
            ListenViewpoint(id: "B", viewpointText: "合成观点 B 超过八个汉字的完整标题", span: "这是足够长度的译稿原文依据片段B", tStart: 18, tEnd: 30, indexInclude: true, sourceKind: "claim", sourcePointId: "claim-B"),
            ListenViewpoint(id: "C", viewpointText: "合成观点 C 超过八个汉字的完整标题", span: "这是足够长度的译稿原文依据片段C", tStart: 40, tEnd: 55, indexInclude: true, sourceKind: "claim", sourcePointId: "claim-C"),
            ListenViewpoint(id: "D", viewpointText: "合成观点 D 超过八个汉字的完整标题", span: "这是足够长度的译稿原文依据片段D", tStart: 95, tEnd: 110, indexInclude: true, sourceKind: "claim", sourcePointId: "claim-D"),
            ListenViewpoint(id: "E", viewpointText: "合成观点 E 超过八个汉字的完整标题", span: "这是足够长度的译稿原文依据片段E", tStart: 130, tEnd: 140, indexInclude: true, sourceKind: "claim", sourcePointId: "claim-E"),
        ]
        let packet = makePacket(claims: claims)
        let index = PointListenIndex(
            workId: "ep-180",
            packetId: "pkt-1",
            transcriptHash: packet.transcriptHash!,
            audioSHA256: audioHash,
            outputVersion: "1",
            timeline: "output.mp3.seconds",
            viewpoints: viewpoints
        )
        return (index, packet)
    }

    func testSelectsDeterministicACEAndUsesAnchorSpanWording() throws {
        let (ep, _, sha) = try makeAudioEpisode()
        let (index, packet) = makeFivePicksFixture(audioHash: sha)
        let picks = ListenPicks.select(
            nodes: index.viewpoints,
            index: index,
            packet: packet,
            episode: ep,
            audioHash: sha,
            duration: 180
        )
        XCTAssertEqual(picks.map(\.id), ["A", "C", "E"])
        XCTAssertEqual(picks[0].anchorSpanLabel, "依据片段约 12 秒")
        XCTAssertEqual(picks[1].anchorSpanLabel, "依据片段约 15 秒")
        XCTAssertEqual(picks[2].anchorSpanLabel, "依据片段约 10 秒")
        XCTAssertEqual(picks[0].reason, "核心观点 · 有译稿依据")
        XCTAssertEqual(picks[2].reason, "已确认观点 · 有译稿依据")
        for pick in picks {
            XCTAssertFalse(pick.reason.contains("AI"))
            XCTAssertFalse(pick.reason.contains("语义"))
        }
    }

    func testRefusesInvalidTimelinesHashesAndSlotMismatches() throws {
        let (ep, _, sha) = try makeAudioEpisode()
        let (index, packet) = makeFivePicksFixture(audioHash: sha)

        // 1) 假时间轴
        var fakeAxis = index
        fakeAxis.timeline = "source.start_ms"
        XCTAssertTrue(ListenPicks.select(nodes: fakeAxis.viewpoints, index: fakeAxis, packet: packet, episode: ep, audioHash: sha, duration: 180).isEmpty)

        // 2) 音频哈希错配
        XCTAssertTrue(ListenPicks.select(nodes: index.viewpoints, index: index, packet: packet, episode: ep, audioHash: String(repeating: "0", count: 64), duration: 180).isEmpty)

        // 3) 仅 1 条有效候选时不凑数
        let singleIndex = PointListenIndex(
            workId: index.workId, packetId: index.packetId, transcriptHash: index.transcriptHash,
            audioSHA256: sha, outputVersion: "1", timeline: "output.mp3.seconds",
            viewpoints: [index.viewpoints[0]]
        )
        XCTAssertTrue(ListenPicks.select(nodes: singleIndex.viewpoints, index: singleIndex, packet: packet, episode: ep, audioHash: sha, duration: 180).isEmpty)

        // 4) 待核听样强行塞入也不产出精选
        let (pendingEp, _, _) = try makeAudioEpisode(pending: .package(id: "ep-180", duration: 180, count: 3))
        XCTAssertTrue(ListenPicks.select(nodes: index.viewpoints, index: index, packet: packet, episode: pendingEp, audioHash: sha, duration: 180).isEmpty)

        // 5) 槽位歧义/错槽/entity 跨槽借标/discarded/lowConfidence 均不命中或被淘汰
        let ambiguousClaim = ExtractClaim(id: "shared-1", text: "c", stance: nil, span: nil, tStart: 10, tEnd: 20, confidence: 0.9, status: .confirmed, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil)
        let ambiguousMethod = ExtractMethod(id: "shared-1", title: "m", steps: nil, params: nil, cautions: nil, span: nil, tStart: 10, tEnd: 20, confidence: 0.9, status: .confirmed, defaultAction: nil, pointType: nil, lowConfidence: nil, uiPrimaryLabel: nil, disclaimer: nil)
        let discardedClaim = ExtractClaim(id: "disc-1", text: "d", stance: nil, span: "足够长的译稿原文依据片段", tStart: 30, tEnd: 40, confidence: 0.9, status: .discarded, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil)
        let lowClaim = ExtractClaim(id: "low-1", text: "l", stance: nil, span: "足够长的译稿原文依据片段", tStart: 50, tEnd: 60, confidence: 0.4, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: true, attrs: nil)
        let methodOnly = ExtractMethod(id: "m-only", title: "m", steps: nil, params: nil, cautions: nil, span: nil, tStart: 70, tEnd: 80, confidence: 0.9, status: .candidate, defaultAction: nil, pointType: nil, lowConfidence: nil, uiPrimaryLabel: nil, disclaimer: nil)
        let p2Packet = makePacket(claims: [ambiguousClaim, discardedClaim, lowClaim], methods: [ambiguousMethod, methodOnly])
        let p2Nodes = [
            // sourceKind == nil 四槽同 id 歧义，且无 span -> 未匹配且无 span 依据，应淘汰
            ListenViewpoint(id: "v1", viewpointText: "足够长的测试观点标题一二三四", span: nil, tStart: 10, tEnd: 20, indexInclude: true, sourceKind: nil, sourcePointId: "shared-1"),
            // sourceKind == "claim" 但只有 method 有 m-only，无 span -> 未匹配，不标实操方法
            ListenViewpoint(id: "v2", viewpointText: "足够长的测试观点标题一二三四", span: nil, tStart: 70, tEnd: 80, indexInclude: true, sourceKind: "claim", sourcePointId: "m-only"),
            // sourceKind == "entity" 即使 claims 有同 id 也严禁跨槽
            ListenViewpoint(id: "v3", viewpointText: "足够长的测试观点标题一二三四", span: nil, tStart: 90, tEnd: 100, indexInclude: true, sourceKind: "entity", sourcePointId: "shared-1"),
            // discarded 即使 span >= 8 仍淘汰
            ListenViewpoint(id: "v4", viewpointText: "足够长的测试观点标题一二三四", span: "足够长的译稿原文依据片段", tStart: 30, tEnd: 40, indexInclude: true, sourceKind: "claim", sourcePointId: "disc-1"),
            // lowConfidence 即使 span >= 8 仍淘汰
            ListenViewpoint(id: "v5", viewpointText: "足够长的测试观点标题一二三四", span: "足够长的译稿原文依据片段", tStart: 50, tEnd: 60, indexInclude: true, sourceKind: "claim", sourcePointId: "low-1"),
        ]
        let p2Index = PointListenIndex(workId: "ep-180", packetId: "pkt-1", transcriptHash: p2Packet.transcriptHash!, audioSHA256: sha, outputVersion: "1", timeline: "output.mp3.seconds", viewpoints: p2Nodes)
        XCTAssertTrue(ListenPicks.select(nodes: p2Nodes, index: p2Index, packet: p2Packet, episode: ep, audioHash: sha, duration: 180).isEmpty)
    }

    func testSubSecondFixtureIsRejectedByMinDuration() throws {
        // P2 #5: 0.5s 锚点夹具不得产出精选
        let (ep, _, sha) = try makeAudioEpisode(id: "point-listen-isolated", duration: 60)
        let claims = [
            ExtractClaim(id: "c1", text: "观点一超过八个字测试", stance: nil, span: "足够长的译稿依据片段一", tStart: 5.0, tEnd: 5.5, confidence: 0.9, status: .confirmed, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
            ExtractClaim(id: "c2", text: "观点二超过八个字测试", stance: nil, span: "足够长的译稿依据片段二", tStart: 25.0, tEnd: 25.5, confidence: 0.9, status: .confirmed, defaultAction: nil, pointType: nil, lowConfidence: nil, attrs: nil),
        ]
        let nodes = [
            ListenViewpoint(id: "v1", viewpointText: "观点一超过八个字测试", span: "足够长的译稿依据片段一", tStart: 5.0, tEnd: 5.5, indexInclude: true, sourceKind: "claim", sourcePointId: "c1"),
            ListenViewpoint(id: "v2", viewpointText: "观点二超过八个字测试", span: "足够长的译稿依据片段二", tStart: 25.0, tEnd: 25.5, indexInclude: true, sourceKind: "claim", sourcePointId: "c2"),
        ]
        let packet = makePacket(workId: "point-listen-isolated", claims: claims)
        let index = PointListenIndex(workId: "point-listen-isolated", packetId: "pkt-1", transcriptHash: packet.transcriptHash!, audioSHA256: sha, outputVersion: "1", timeline: "output.mp3.seconds", viewpoints: nodes)
        XCTAssertTrue(ListenPicks.select(nodes: nodes, index: index, packet: packet, episode: ep, audioHash: sha, duration: 60).isEmpty)
    }

    func testEpisodeContentStatusConservativeMatrixAndChipCap() throws {
        // 中文成片（tts 或完整 revision 或老集侧车），人审恒为 pending
        let (masterEp, _, _) = try makeAudioEpisode(audioKind: "tts", hasChineseRenderSidecar: true)
        let masterStatus = EpisodeContentStatus.resolve(episode: masterEp)
        XCTAssertEqual(masterStatus.kind, .chineseMaster)
        XCTAssertEqual(masterStatus.humanReview, .pending)
        XCTAssertEqual(masterStatus.reviewLabel, "未人审")
        XCTAssertNil(masterStatus.listBadgeLabel)

        // 仅有 output.mp3（缺侧车、缺 mediaRevision、缺 audioKind）-> 保守来源待核
        let (bareEp, _, _) = try makeAudioEpisode(audioKind: nil, hasChineseRenderSidecar: false)
        let bareStatus = EpisodeContentStatus.resolve(episode: bareEp)
        XCTAssertEqual(bareStatus.kind, .unverifiedAudio)
        XCTAssertEqual(bareStatus.primaryLabel, "来源待核")
        XCTAssertEqual(bareStatus.listBadgeLabel, "来源待核")

        // 待核听样
        let (pendingEp, _, _) = try makeAudioEpisode(audioKind: "original_audio", pending: .package(id: "ep-180", duration: 180, count: 2), hasChineseRenderSidecar: false)
        XCTAssertEqual(EpisodeContentStatus.resolve(episode: pendingEp).kind, .pendingSample)
        XCTAssertEqual(EpisodeContentStatus.resolve(episode: pendingEp).listBadgeLabel, "待核听样")

        // 不可用音频
        let (unavailEp, _, _) = try makeAudioEpisode(unavailable: .audio)
        XCTAssertEqual(EpisodeContentStatus.resolve(episode: unavailEp).kind, .unavailableAudio)

        // 列表芯片上限不挤占原有 3 枚徽章
        XCTAssertNil(LibraryShelf.appendedStatusChip(existingCount: 3, statusLabel: "来源待核"))
        XCTAssertEqual(LibraryShelf.appendedStatusChip(existingCount: 2, statusLabel: "待核听样"), "待核听样")
        XCTAssertNil(LibraryShelf.appendedStatusChip(existingCount: 1, statusLabel: masterStatus.listBadgeLabel))
    }

    func testSegmentPlaybackTrackerAndFollowStateDoNotHijackOrDeadlock() {
        let nodes = [
            ListenViewpoint(id: "A", viewpointText: "A", span: nil, tStart: 10, tEnd: 20, indexInclude: true, sourceKind: nil, sourcePointId: nil),
            ListenViewpoint(id: "B", viewpointText: "B", span: nil, tStart: 18, tEnd: 30, indexInclude: true, sourceKind: nil, sourcePointId: nil),
            ListenViewpoint(id: "C", viewpointText: "C", span: nil, tStart: 40, tEnd: 55, indexInclude: true, sourceKind: nil, sourcePointId: nil),
        ]

        // 片头未入首段 -> nil
        XCTAssertNil(SegmentPlaybackTracker.activeNodeID(at: 5, in: nodes, duration: 180))
        // 精确命中 A
        XCTAssertEqual(SegmentPlaybackTracker.activeNodeID(at: 12, in: nodes, duration: 180), "A")
        // 重叠区优先新段 B
        XCTAssertEqual(SegmentPlaybackTracker.activeNodeID(at: 19, in: nodes, duration: 180), "B")
        // 间隙保持最近经过段 B
        XCTAssertEqual(SegmentPlaybackTracker.activeNodeID(at: 35, in: nodes, duration: 180), "B")
        // 外层门禁关闭
        XCTAssertNil(SegmentPlaybackTracker.activeNodeID(at: 12, in: nodes, duration: 180, isCurrentEpisode: false, isAudioCurrent: true, isPendingTranscript: false, isUnavailable: false))
        XCTAssertNil(SegmentPlaybackTracker.activeNodeID(at: 12, in: nodes, duration: 180, isCurrentEpisode: true, isAudioCurrent: false, isPendingTranscript: false, isUnavailable: false))

        // 防抢滚与同段回滚闭环
        var state = SegmentScrollFollowState()
        XCTAssertTrue(state.shouldAutoScroll(to: "A"))
        state.markAutoScrolled("A")
        XCTAssertFalse(state.shouldAutoScroll(to: "A"))

        state.userDidScroll()
        XCTAssertFalse(state.isFollowing)
        XCTAssertFalse(state.shouldAutoScroll(to: "B"))
        XCTAssertTrue(state.showsBackToCurrentButton(activeID: "A"))
        XCTAssertFalse(state.showsBackToCurrentButton(activeID: nil))

        // returnToCurrent 必须清 lastAutoScrolledID，保证同段也能回滚
        state.returnToCurrent()
        XCTAssertTrue(state.isFollowing)
        XCTAssertNil(state.lastAutoScrolledID)
        XCTAssertTrue(state.shouldAutoScroll(to: "A"))
    }
}
