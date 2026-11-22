import XCTest
@testable import PrivateListen

@MainActor
final class ListenMarkAliasTests: XCTestCase {
    private let revisionA = String(repeating: "a", count: 64)
    private let revisionB = String(repeating: "b", count: 64)

    func testBareIDWithoutRevisionKeyResumesAndShowsContinue() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let episode = makeEpisode(id: "ep-legacy", revision: revisionA, duration: 1800)
        try seedProgress(defaults, [episode.id: mark(seconds: 156, finished: false)])
        let loaded = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertEqual(loaded.resumeSeconds(for: episode), 156)
        XCTAssertFalse(loaded.isFinished(episode))
        XCTAssertEqual(loaded.continueEntries(in: [episode]).map(\.id), [episode.id])
        XCTAssertEqual(loaded.continueEntries(in: [episode]).first?.seconds, 156)
    }

    func testRevisionKeyPreferredOverConflictingBareID() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let episode = makeEpisode(id: "ep-conflict", revision: revisionA, duration: 1800)
        try seedProgress(defaults, [
            episode.id: mark(seconds: 156, finished: false),
            episode.mediaKey: mark(seconds: 10, finished: false),
        ])
        let loaded = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertEqual(loaded.resumeSeconds(for: episode), 10)
        XCTAssertEqual(loaded.continueEntries(in: [episode]).first?.seconds, 10)
        XCTAssertFalse(loaded.isFinished(episode))
    }

    func testBareIDDoesNotMapAcrossADifferentRevision() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let current = makeEpisode(id: "ep-unmap", revision: revisionB, duration: 1800)
        try seedProgress(defaults, [
            current.id: mark(seconds: 156, finished: false),
            "\(current.id).\(revisionA)": mark(seconds: 40, finished: false),
        ])
        let loaded = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertEqual(loaded.resumeSeconds(for: current), 0)
        XCTAssertTrue(loaded.continueEntries(in: [current]).isEmpty)
        XCTAssertFalse(loaded.isFinished(current))
    }

    func testDismissOfLegacyContinueSurvivesReloadWithoutClearingProgress() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let episode = makeEpisode(id: "ep-dismiss", revision: revisionA, duration: 1800)
        let other = makeEpisode(id: "ep-other", revision: nil, duration: 600)
        try seedProgress(defaults, [
            episode.id: mark(seconds: 156, finished: false),
            other.id: mark(seconds: 20, finished: false),
        ])
        try seedTouched(defaults, [episode.id: 1_000, other.id: 900])
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertEqual(store.continueEntries(in: [episode, other]).map(\.id), [episode.id])
        store.dismissContinue(episode.id)
        XCTAssertEqual(store.resumeSeconds(for: episode), 156)
        XCTAssertFalse(store.isFinished(episode))
        XCTAssertEqual(store.continueEntries(in: [episode, other]).map(\.id), [other.id])

        let reopened = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertTrue(reopened.continueEntries(in: [episode, other]).map(\.id) == [other.id],
                      "dismissed legacy card must not resurrect from the bare-id fallback")
        XCTAssertEqual(reopened.resumeSeconds(for: episode), 156)
        XCTAssertFalse(reopened.isFinished(episode))
        XCTAssertEqual(reopened.resumeSeconds(for: other), 20)

        reopened.acknowledgeContinue(episode)
        XCTAssertEqual(reopened.continueEntries(in: [episode, other]).map(\.id), [episode.id])
        XCTAssertEqual(reopened.resumeSeconds(for: episode), 156)
    }

    func testDismissBareIDStillHidesWhenRevisionKeyAlsoExists() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let episode = makeEpisode(id: "ep-both", revision: revisionA, duration: 1800)
        try seedProgress(defaults, [
            episode.id: mark(seconds: 156, finished: false),
            episode.mediaKey: mark(seconds: 80, finished: false),
        ])
        defaults.set([episode.id], forKey: "PrivateListen.continueDismissed")
        let loaded = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        XCTAssertTrue(loaded.continueEntries(in: [episode]).isEmpty)
        XCTAssertEqual(loaded.resumeSeconds(for: episode), 80)
        XCTAssertFalse(loaded.isFinished(episode))
    }

    func testRememberWritesRevisionKeyAndLeavesBareIDSource() throws {
        let (_, defaults, suite) = makeStore()
        defer { defaults.removePersistentDomain(forName: suite) }
        let episode = makeEpisode(id: "ep-write", revision: revisionA, duration: 1800)
        try seedProgress(defaults, [episode.id: mark(seconds: 156, finished: false)])
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        _ = store.remember(episode: episode, seconds: 160, duration: 1800, finished: false)
        XCTAssertEqual(store.resumeSeconds(for: episode), 160)
        let persisted = try JSONDecoder().decode(
            [String: LibraryStore.ListenMark].self,
            from: XCTUnwrap(defaults.data(forKey: "PrivateListen.progress"))
        )
        XCTAssertEqual(persisted[episode.id]?.seconds, 156)
        XCTAssertEqual(persisted[episode.mediaKey]?.seconds, 160)
        XCTAssertEqual(persisted[episode.id]?.finished, false)
        XCTAssertEqual(persisted[episode.mediaKey]?.finished, false)
    }

    private func makeStore() -> (LibraryStore, UserDefaults, String) {
        let suite = "ListenMarkAliasTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return (LibraryStore(defaults: defaults, loadLibraryOnInit: false), defaults, suite)
    }

    private func makeEpisode(id: String, revision: String?, duration: Double) -> Episode {
        Episode(
            id: id,
            title: id,
            guests: [],
            lensIds: [],
            createdAt: .distantPast,
            durationSec: duration,
            audioURL: URL(fileURLWithPath: "/tmp/\(id).mp3"),
            downloading: false,
            mediaRevision: revision
        )
    }

    private func mark(seconds: Double, finished: Bool) -> LibraryStore.ListenMark {
        LibraryStore.ListenMark(seconds: seconds, finished: finished, finishedSource: nil, replaySeconds: nil)
    }

    private func seedProgress(_ defaults: UserDefaults, _ marks: [String: LibraryStore.ListenMark]) throws {
        defaults.set(try JSONEncoder().encode(marks), forKey: "PrivateListen.progress")
    }

    private func seedTouched(_ defaults: UserDefaults, _ values: [String: TimeInterval]) throws {
        defaults.set(try JSONEncoder().encode(values), forKey: "PrivateListen.continueTouched")
    }
}
