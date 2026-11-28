#if !os(iOS) || DEBUG
import Foundation
#if os(iOS)
@testable import PrivateListen
#endif

@MainActor
enum LibrarySnapshotChecks {
    struct Failure: Error, CustomStringConvertible {
        let description: String
    }

    static func runReadAvailability() async throws {
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let listing = libraryListing(scratch)
        let root = listing.deletingLastPathComponent()
        let fixture = try stagingPackage(id: "degraded", title: "Fault injection", audio: "current audio", extras: "current",
                                         additionalFiles: ["cover.jpg": Data("cover".utf8),
                                                           "point-listen.json": Data("point index".utf8)])
        let relative = "objects/\(fixture.revision)"
        let folder = root.appendingPathComponent(relative, isDirectory: true)
        let previous = "episodes/\(fixture.id)"
        let other = ["id": "healthy", "title": "Healthy episode"]
        for (path, audio) in [(previous, "previous audio"), ("episodes/healthy", "independent audio")] {
            let directory = root.appendingPathComponent(path, isDirectory: true)
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            try Data(audio.utf8).write(to: directory.appendingPathComponent("output.mp3"))
        }
        let (store, defaults, suite) = try isolatedStore(scratch)
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? fm.removeItem(at: scratch)
        }
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        for (name, data) in fixture.files { try data.write(to: folder.appendingPathComponent(name)) }
        var row: [String: Any] = ["id": fixture.id, "title": "Fault injection", "artwork": "cover.jpg",
                                  "duration_sec": 90, "file": "output.mp3"]
        let revision: [String: Any] = [
            "sha256": fixture.revision, "path": relative, "previous_path": previous,
            "resume_policy": "reset_unmapped", "files": fixture.files.mapValues(LibrarySnapshot.digest),
        ]
        row["media_revision"] = revision
        let catalog: [String: Any] = ["schema": PushStaging.schema, "generation": 2, "episodes": [row, other]]
        let healthyBytes = try json(catalog)
        let digest = try store.commitLocalSnapshot(healthyBytes, expecting: nil)
        guard let target = store.episodes.first(where: { $0.id == fixture.id }) else {
            throw Failure(description: "healthy revision must load")
        }
        try expect(store.episodes.count == 2 && store.episodes.allSatisfy(\.isPlayableNow)
                   && target.artworkURL != nil && target.publishedAsset("point-listen.json") != nil,
                   "healthy catalog makes both episodes and optional assets available")
        _ = store.remember(episode: target, seconds: 20, duration: 90, finished: false)
        let progress = defaults.data(forKey: "PrivateListen.progress")

        let cover = folder.appendingPathComponent("cover.jpg")
        let point = folder.appendingPathComponent("point-listen.json")
        try fm.removeItem(at: cover)
        try Data("wrong point bytes".utf8).write(to: point)
        let lightweightOptional = LibraryStore.loadEpisodes(from: root, localCopy: true)
        let lightweightTarget = lightweightOptional.episodes.first { $0.id == target.id }
        try expect(lightweightOptional.accepted && lightweightOptional.episodes.count == 2 &&
                   lightweightTarget?.isPlayableNow == true && lightweightTarget?.artworkURL == nil,
                   "missing cover takes effect immediately in lightweight load while episode stays playable")
        store.reload()
        await store.waitForVerification()
        let noOptionalAssets = store.episodes.first { $0.id == target.id }
        let fullOptionalDamage = LibraryStore.loadEpisodes(from: root, localCopy: true, verification: .full)
        let fullNoOptionalAssets = fullOptionalDamage.episodes.first { $0.id == target.id }
        try expect(noOptionalAssets?.isPlayableNow == true && noOptionalAssets?.artworkURL == nil &&
                   noOptionalAssets?.publishedAsset("point-listen.json") == nil &&
                   fullOptionalDamage.accepted && fullNoOptionalAssets?.isPlayableNow == true &&
                   fullNoOptionalAssets?.artworkURL == nil && fullNoOptionalAssets?.publishedAsset("point-listen.json") == nil,
                   "missing cover and wrong-hash point index leave the episode playable")
        try refuses { try store.commitLocalSnapshot(healthyBytes, expecting: digest) }
        try fixture.files["cover.jpg"]!.write(to: cover)
        try fixture.files["point-listen.json"]!.write(to: point)

        let faults: [(String, Data?, Episode.UnavailableReason)] = [
            ("output.mp3", nil, .audio),
            ("output.mp3", Data("wrong audio bytes".utf8), .audio),
            ("document.json", Data("wrong critical revision bytes".utf8), .revision),
        ]
        for (name, brokenBytes, reason) in faults {
            let asset = folder.appendingPathComponent(name)
            try fm.removeItem(at: asset)
            if let brokenBytes { try brokenBytes.write(to: asset) }
            store.reload()
            if brokenBytes == nil {
                let immediateUnavailable = store.episodes.first { $0.id == target.id }
                try expect(store.episodes.count == 2 && store.episodes.filter(\.isPlayableNow).count == 1
                           && immediateUnavailable?.unavailableReason == reason && immediateUnavailable?.audioURL == target.audioURL,
                           "missing critical asset is synchronously unavailable on reload")
            }
            await store.waitForVerification()
            let unplayable = store.episodes.first { $0.id == target.id }
            try expect(store.episodes.count == 2 && store.episodes.filter(\.isPlayableNow).count == 1
                       && unplayable?.unavailableReason == reason && unplayable?.audioURL == target.audioURL,
                       "critical asset failure retains the row and the current pointer while disabling playback")
            _ = store.remember(episode: target, seconds: 50, duration: 90, finished: false)
            try expect(defaults.data(forKey: "PrivateListen.progress") == progress,
                       "a stale playback callback cannot overwrite protected progress")
            try refuses { try store.commitLocalSnapshot(healthyBytes, expecting: digest) }
            try expect(LibrarySnapshot.digest(Data(contentsOf: listing)) == digest,
                       "strict commit does not replace the catalog under asset damage")
            try fixture.files[name]!.write(to: asset)
        }

        let legacyAudio = root.appendingPathComponent("episodes/healthy/output.mp3")
        try fm.removeItem(at: legacyAudio)
        for localCopy in [true, false] {
            let loaded = LibraryStore.loadEpisodes(from: root, localCopy: localCopy)
            try expect(loaded.accepted && loaded.episodes.filter(\.isPlayableNow).count == 1
                       && loaded.episodes.first(where: { $0.id == "healthy" })?.unavailableReason == .audio,
                       "absent legacy audio is unavailable for both readers")
        }
        try Data("independent audio".utf8).write(to: legacyAudio)

        var unsafeRevision = revision
        unsafeRevision["path"] = "../\(relative)"
        for badRevision in [unsafeRevision as Any, "malformed revision" as Any] {
            var bad = catalog
            row["media_revision"] = badRevision
            bad["episodes"] = [row, other]
            try json(bad).write(to: listing)
            let loaded = LibraryStore.loadEpisodes(from: root, localCopy: true)
            let unavailable = loaded.episodes.first { $0.id == target.id }
            try expect(loaded.accepted && loaded.episodes.count == 2 && loaded.episodes.filter(\.isPlayableNow).count == 1
                       && unavailable?.unavailableReason == .revision && unavailable?.publishedAsset("output.mp3") == nil,
                       "unsafe or malformed revision records cannot fall back to the old audio")
        }
        for (key, value) in [("schema", "wrong.schema" as Any), ("generation", 0 as Any),
                             ("generation", true as Any), ("generation", "2" as Any),
                             ("generation", 2.5 as Any), ("generation", NSNull() as Any)] {
            var bad = catalog
            bad[key] = value
            try json(bad).write(to: listing)
            try expect(!LibraryStore.loadEpisodes(from: root, localCopy: true).accepted,
                       "invalid schema or generation rejects the whole catalog")
        }
    }

    static func run() async throws {
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let root = scratch.appendingPathComponent("中文播客私人库", isDirectory: true)
        let listing = root.appendingPathComponent("library.json")
        let suite = "PrivateListen.snapshot-fixture.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { throw Failure(description: "isolated defaults") }
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? fm.removeItem(at: scratch)
        }
        let id = "step3-synthetic"
        let legacy = root.appendingPathComponent("episodes/\(id)", isDirectory: true)
        let oldAudio = legacy.appendingPathComponent("output.mp3")
        let oldText = legacy.appendingPathComponent("document.json")
        try fm.createDirectory(at: legacy, withIntermediateDirectories: true)
        try Data("legacy audio".utf8).write(to: oldAudio)
        try Data("legacy transcript".utf8).write(to: oldText)
        try json(["id": id, "title": "Unpublished sidecar title"]).write(to: legacy.appendingPathComponent("episode.json"))
        let orphan = root.appendingPathComponent("episodes/orphan", isDirectory: true)
        try fm.createDirectory(at: orphan, withIntermediateDirectories: true)
        try json(["id": "orphan", "title": "In transit"]).write(to: orphan.appendingPathComponent("episode.json"))
        try Data("orphan".utf8).write(to: orphan.appendingPathComponent("output.mp3"))
        let phoneOnly: [String: Any] = ["nested": ["mobile", 7, NSNull()]]
        let oldRow: [String: Any] = [
            "id": id, "title": "Catalog title A", "file": "output.mp3", "duration_sec": 120,
            "artwork": "cover.jpg", "artwork_kind": "title", "resume_seconds": 91,
            "phone_only": phoneOnly, "show": "Fixture show", "lens_ids": ["fixture-lens"],
        ]
        let before = try json(["episodes": [oldRow], "phone_settings": ["enabled": true]])
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: scratch)
        store.reload()
        try expect(store.episodes.isEmpty, "no catalog without library.json")
        let initialDigest = try store.commitLocalSnapshot(before, expecting: nil)
        let beforeBytes = try Data(contentsOf: listing)
        try expect(initialDigest == LibrarySnapshot.digest(beforeBytes), "CAS can initialize the first local snapshot")
        guard let old = store.episodes.first else { throw Failure(description: "legacy catalog row") }
        try expect(store.episodes.count == 1 && old.title == "Catalog title A", "catalog wins over sidecar and orphan")
        let cloud = LibraryStore.loadEpisodes(from: root, localCopy: false)
        try expect(cloud.accepted && cloud.episodes.map(\.title) == ["Catalog title A"], "iCloud reader uses the same catalog path")
        store.remember(id: id, seconds: 51, duration: 120, finished: false)
        let oldHandle = try FileHandle(forReadingFrom: old.audioURL)
        defer { try? oldHandle.close() }

        let payloads = [
            "output.mp3": Data("new audio".utf8), "document.json": Data("new transcript".utf8),
            "source-identity.json": Data("new source identity".utf8),
            "render-identity.json": Data("new render timeline".utf8), "episode.json": Data("new episode sidecar".utf8),
        ]
        let files = payloads.mapValues(LibrarySnapshot.digest)
        let fileLines = files.sorted(by: { $0.key < $1.key }).map { "  \"\($0.key)\": \"\($0.value)\"" }
        let revisionID = LibrarySnapshot.digest(Data(("{\n" + fileLines.joined(separator: ",\n") + "\n}\n").utf8))
        let relative = "revisions/\(id)/\(revisionID)"
        let revisionURL = root.appendingPathComponent(relative, isDirectory: true)
        let revision: [String: Any] = [
            "sha256": revisionID, "path": relative, "previous_path": "episodes/\(id)",
            "resume_policy": "reset_unmapped", "files": files,
        ]
        let newRow: [String: Any] = [
            "id": id, "title": "Catalog title B", "duration_sec": 120, "file": "output.mp3",
            "media_revision": revision,
        ]
        // A second row already has local media, but it belongs only to the new snapshot.
        let secondRow = ["id": "orphan", "title": "Committed second row"]
        let proposed = try json(["episodes": [newRow, secondRow]])
        try fm.createDirectory(at: revisionURL, withIntermediateDirectories: true)
        try payloads["output.mp3"]!.write(to: revisionURL.appendingPathComponent("output.mp3"))
        try refuses { try store.commitLocalSnapshot(proposed, expecting: initialDigest) }
        try expect(Data(contentsOf: listing) == beforeBytes, "partial object leaves old snapshot bytes")
        store.reload()
        try expect(store.episodes.first == old && store.resumeSeconds(for: id) == 51, "partial object leaves old catalog and resume active")

        // Readers keep the current rows while verification fails for a single revision.
        try proposed.write(to: listing, options: .atomic)
        store.reload()
        try expect(store.episodes.map(\.id) == [id, "orphan"] && store.episodes.first?.unavailableReason == .revision,
                   "reload shows the current incomplete revision without using the previous audio")
        let cold = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: scratch)
        cold.reload()
        try expect(cold.episodes.count == 2 && cold.episodes.first?.isPlayableNow == false,
                   "cold start lists both rows and disables incomplete media")
        let cloudPartial = LibraryStore.loadEpisodes(from: root, localCopy: false)
        try expect(cloudPartial.accepted && cloudPartial.episodes.first?.unavailableReason == .revision,
                   "iCloud also shows the current incomplete revision")
        try beforeBytes.write(to: listing, options: .atomic)

        for (name, data) in payloads { try data.write(to: revisionURL.appendingPathComponent(name)) }
        store.reload()
        try expect(store.episodes.first == old, "unreferenced complete object is invisible")
        let draft = revisionURL.appendingPathComponent("document.json")
        try Data("mismatched transcript".utf8).write(to: draft)
        try refuses { try store.commitLocalSnapshot(proposed, expecting: initialDigest) }
        try expect(Data(contentsOf: listing) == beforeBytes, "wrong transcript hash leaves the old snapshot")
        try payloads["document.json"]!.write(to: draft)

        var wrongRevision = revision
        wrongRevision["path"] = "../\(relative)"
        var invalid = newRow
        invalid["media_revision"] = wrongRevision
        let unsafe = try json(["episodes": [invalid]])
        try refuses { try store.commitLocalSnapshot(unsafe, expecting: initialDigest) }
        try fm.removeItem(at: draft)
        try fm.createSymbolicLink(at: draft, withDestinationURL: oldText)
        try refuses { try store.commitLocalSnapshot(proposed, expecting: initialDigest) }
        try fm.removeItem(at: draft)
        try payloads["document.json"]!.write(to: draft)
        try expect(Data(contentsOf: listing) == beforeBytes, "path traversal and symlink do not advance catalog")

        let nextDigest = try store.commitLocalSnapshot(proposed, expecting: initialDigest)
        guard let new = store.episodes.first else { throw Failure(description: "revision object was not selected") }
        let committed = try Data(contentsOf: listing)
        try expect(store.episodes.map(\.id) == [id, "orphan"], "whole new snapshot is visible after one replacement")
        try expect(new.publishedAsset("document.json") != nil && new.publishedAsset("extract.live.json") == nil,
                   "revision consumers can open only manifest-listed sidecars")
        try expect(new.id == old.id && !new.matchesMedia(old) && new.mediaRevision == revisionID
                   && new.title == "Catalog title B" && new.show == "Fixture show" && new.lensIds == ["fixture-lens"],
                   "same ID switches to complete revision and retains extension metadata")
        try expect(new.audioURL == revisionURL.resolvingSymlinksInPath().appendingPathComponent("output.mp3"),
                   "playback opens the catalog revision directory")
        try expect(Data(contentsOf: new.audioURL) == payloads["output.mp3"], "new handle reads new audio")
        try expect(Data(contentsOf: draft) == payloads["document.json"], "transcript is next to that exact audio")
        try expect(oldHandle.readToEnd() == Data("legacy audio".utf8), "open old playback handle keeps old bytes")
        try expect(Data(contentsOf: old.audioURL) == Data("legacy audio".utf8), "old URL remains reusable")
        try expect(Data(contentsOf: oldText) == Data("legacy transcript".utf8), "previous path remains immutable")
        let adoptedContinue = store.continueEntries(in: store.episodes).first
        try expect(store.resumeSeconds(for: id) == 51 && adoptedContinue?.id == id && adoptedContinue?.seconds == 51,
                   "the first revision adopts the bare-ID mark and appears in Continue")
        store.remember(episode: old, seconds: 66, duration: 120, finished: false)
        try expect(store.resumeSeconds(for: new) == 51 && store.resumeSeconds(for: old) == 66,
                   "outgoing playback callback cannot overwrite new progress")
        store.remember(episode: new, seconds: 9, duration: 120, finished: false)
        let reopened = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: scratch)
        reopened.reload()
        try expect(reopened.resumeSeconds(for: id) == 9 && reopened.resumeSeconds(for: old) == 66,
                   "both progress keys persist across store reload and restart")
        reopened.dismissContinue(id)
        try expect(reopened.continueEntries(in: reopened.episodes).isEmpty && reopened.resumeSeconds(for: id) == 9,
                   "left swipe hides this revision's Continue row and retains its position")

        let raw = try JSONSerialization.jsonObject(with: committed) as? [String: Any]
        let row = (raw?["episodes"] as? [[String: Any]])?.first
        try expect((row?["phone_only"] as? NSDictionary) == (phoneOnly as NSDictionary),
                   "phone-only nested fields survive comparison and disk replacement")
        try expect((raw?["phone_settings"] as? [String: Bool])?["enabled"] == true,
                   "phone-only top-level fields survive")
        try expect(row?["resume_seconds"] == nil && row?["artwork"] == nil, "publisher removals are honored")
        try expect(store.commitLocalSnapshot(proposed, expecting: initialDigest) == nextDigest,
                   "same complete payload is replay-safe with original CAS token")
        var loser = newRow
        loser["title"] = "Stale concurrent writer"
        let conflict = try json(["episodes": [loser, secondRow]])
        do {
            _ = try reopened.commitLocalSnapshot(conflict, expecting: initialDigest)
            throw Failure(description: "stale competing CAS unexpectedly succeeded")
        } catch LibrarySnapshotError.staleCatalog {
            print("PASS stale competing writer rejected")
        }
        try expect(Data(contentsOf: listing) == committed, "stale and replayed CAS do not rewrite index bytes")

        try Data("{\"episodes\":false}".utf8).write(to: listing)
        store.reload()
        try expect(store.episodes.first == new, "corrupt catalog reload keeps previously accepted snapshot")
        try refuses { try store.commitLocalSnapshot(proposed, expecting: nil) }
        try expect(Data(contentsOf: listing) == Data("{\"episodes\":false}".utf8), "CAS fails closed on a malformed prior index")
        try runIngestContract()
        try await runVerificationAndStreamingChecks()
    }

    static func runIngestContract() throws {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("private-listen-ingest-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }

        try ingestMultiEpisodeInterrupt(in: scratch)
        try ingestMidInstallKeepsOldLibrary(in: scratch)
        try ingestCommitBoundary(in: scratch)
        try ingestRevisionKeepsOldHandle(in: scratch)
        try ingestSerialRemerge(in: scratch)
        try ingestLostReceiptRetry(in: scratch)
        try ingestFailClosed(in: scratch)
        try migrateLegacyInterruptRetry(in: scratch)
        print("PASS official D3 ingest eight-category contract")
    }

    static func consumeAdapterDocuments(_ documents: URL) throws {
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let receipts = try store.consumePushStaging()
        try expect(!receipts.isEmpty, "isolated Python adapter staging is consumed")
        try expect(store.episodes.contains(where: { $0.id == "ep-adapter" && $0.title == "Adapter Episode" }),
                   "adapter-written package is visible after unique catalog commit")
        let listing = libraryListing(documents)
        let snapshot = try LibrarySnapshot(Data(contentsOf: listing))
        try expect(snapshot.generation == 1, "adapter ingest stamps generation 1")
        try expect(snapshot.transactions["adapter-tx-1"] != nil, "adapter tx is in the catalog ledger")
        try expect(store.episodes.contains(where: { $0.mediaRevision != nil && ($0.audioURL.path as NSString).contains("objects/") }),
                   "adapter package landed under objects/<revision>")
        let again = try store.consumePushStaging()
        try expect(again.count == 1 && again[0].rebuilt && again[0].resultDigest == receipts[0].resultDigest,
                   "adapter tx retry is idempotent from the ledger")
        print("PASS isolated adapter staging consumed by App ingest (not a USB pass)")
    }

    private static func ingestMultiEpisodeInterrupt(in root: URL) throws {
        let documents = root.appendingPathComponent("multi", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = try stagingPackage(id: "ep-one", title: "Episode One", audio: "audio-one", extras: "one")
        let second = try stagingPackage(id: "ep-two", title: "Episode Two", audio: "audio-two", extras: "two")
        try writeStaging(documents: documents, txId: "tx-multi", packages: [first, second])
        try ingestRefuses(store, fault: .midInstall, "multi-episode mid-file interrupt fails closed")
        store.reload()
        try expect(store.episodes.isEmpty, "multi-episode interrupt does not leak titles into the visible library")
        try expect(!FileManager.default.fileExists(atPath: libraryListing(documents).path),
                   "multi-episode interrupt leaves the catalog uncommitted")
        let receipts = try store.consumePushStaging()
        try expect(receipts.count == 1 && receipts[0].txId == "tx-multi" && !receipts[0].rebuilt,
                   "retry of the interrupted multi-episode tx commits once")
        try expect(Set(store.episodes.map(\.id)) == Set(["ep-one", "ep-two"]),
                   "retry after interrupt publishes both episodes from one catalog replace")
        try expect(store.episodes.allSatisfy { $0.audioURL.path.contains("/objects/") },
                   "multi-episode commit installs under objects/<revision>")
    }

    private static func ingestMidInstallKeepsOldLibrary(in root: URL) throws {
        let documents = root.appendingPathComponent("mid-install", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let original = try stagingPackage(id: "ep-keep", title: "Kept Title", audio: "kept-audio", extras: "keep")
        try writeStaging(documents: documents, txId: "tx-keep", packages: [original])
        let baselineReceipts = try store.consumePushStaging()
        try expect(baselineReceipts.count == 1, "seed catalog commits before mid-install fault")
        let before = try Data(contentsOf: libraryListing(documents))
        let beforeTitle = store.episodes[0].title
        let next = try stagingPackage(id: "ep-new", title: "Should Not Appear", audio: "new-audio", extras: "new")
        try writeStaging(documents: documents, txId: "tx-mid", packages: [next])
        try ingestRefuses(store, fault: .midInstall, "kill mid resource install fails closed")
        store.reload()
        try expect(Data(contentsOf: libraryListing(documents)) == before,
                   "kill mid resource install leaves the previous library.json bytes")
        try expect(store.episodes.map(\.title) == [beforeTitle],
                   "kill mid resource install still presents only the old catalog")
        try expect(!store.episodes.contains(where: { $0.id == "ep-new" }),
                   "uncommitted mid-install package is not visible")
    }

    private static func ingestCommitBoundary(in root: URL) throws {
        let documents = root.appendingPathComponent("commit-boundary", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = try stagingPackage(id: "ep-bound", title: "Before Commit", audio: "bound-a", extras: "a")
        try writeStaging(documents: documents, txId: "tx-before", packages: [first])
        try ingestRefuses(store, fault: .beforeCommit, "kill before catalog replace fails closed")
        store.reload()
        try expect(store.episodes.isEmpty && !FileManager.default.fileExists(atPath: libraryListing(documents).path),
                   "kill before catalog replace keeps the old (absent) library")

        let receipts = try store.consumePushStaging()
        try expect(receipts.count == 1 && store.episodes.map(\.id) == ["ep-bound"],
                   "retry after before-commit fault commits the new catalog")
        let committed = try Data(contentsOf: libraryListing(documents))

        let second = try stagingPackage(id: "ep-after", title: "After Commit", audio: "bound-b", extras: "b")
        try writeStaging(documents: documents, txId: "tx-after", packages: [second])
        try ingestRefuses(store, fault: .afterCommitBeforeReceipt,
                          "kill after catalog replace before receipt fails after the unique commit")
        store.reload()
        let after = try Data(contentsOf: libraryListing(documents))
        try expect(after != committed, "after-commit fault already replaced library.json")
        try expect(Set(store.episodes.map(\.id)) == Set(["ep-bound", "ep-after"]),
                   "after-commit fault presents only the new complete catalog")
        let receiptURL = documents.appendingPathComponent(".push-staging/tx-after/receipt.json")
        try expect(!FileManager.default.fileExists(atPath: receiptURL.path),
                   "after-commit fault did not write receipt.json")
        let rebuilt = try store.rebuildReceipt(txId: "tx-after")
        try expect(rebuilt.rebuilt && rebuilt.txId == "tx-after",
                   "lost receipt is rebuilt from the catalog ledger without re-applying")
        try expect(FileManager.default.fileExists(atPath: receiptURL.path),
                   "rebuilt receipt is written next to the original staging tx")
        try expect(Data(contentsOf: libraryListing(documents)) == after,
                   "receipt rebuild does not rewrite the committed catalog")
    }

    private static func ingestRevisionKeepsOldHandle(in root: URL) throws {
        let documents = root.appendingPathComponent("old-handle", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let v1 = try stagingPackage(id: "ep-rev", title: "Revision One", audio: "audio-v1", extras: "v1")
        try writeStaging(documents: documents, txId: "tx-rev-1", packages: [v1])
        _ = try store.consumePushStaging()
        guard let original = store.episodes.first(where: { $0.id == "ep-rev" }) else {
            throw Failure(description: "first revision did not load")
        }
        _ = store.remember(episode: original, seconds: 17, duration: 120, finished: false)
        let handle = try FileHandle(forReadingFrom: original.audioURL)
        defer { try? handle.close() }
        let v1Bytes = try Data(contentsOf: original.audioURL)
        let v1Revision = original.mediaRevision
        let v2 = try stagingPackage(id: "ep-rev", title: "Revision Two", audio: "audio-v2", extras: "v2",
                                    previous: "objects/\(v1.revision)")
        try writeStaging(documents: documents, txId: "tx-rev-2", packages: [v2])
        _ = try store.consumePushStaging()
        guard let updated = store.episodes.first(where: { $0.id == "ep-rev" }) else {
            throw Failure(description: "second revision did not load")
        }
        try expect(updated.mediaRevision != v1Revision && updated.title == "Revision Two",
                   "same-id content revision publishes the new objects path")
        try expect(store.resumeSeconds(for: updated) == 0,
                   "new revision does not inherit unmapped playhead seconds")
        try expect(store.resumeSeconds(for: original) == 17,
                   "old revision media key still holds its playhead")
        let after = handle.readDataToEndOfFile()
        try expect(after == v1Bytes && LibrarySnapshot.digest(after) == LibrarySnapshot.digest(v1Bytes),
                   "open old media handle still reads the original SHA")
        try expect(Data(contentsOf: original.audioURL) == v1Bytes,
                   "old objects revision is never overwritten")
        try expect(Data(contentsOf: updated.audioURL) != v1Bytes,
                   "new handle reads the new audio bytes")
    }

    private static func ingestSerialRemerge(in root: URL) throws {
        let documents = root.appendingPathComponent("remerge", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let listenAlong = documents.appendingPathComponent("listen_along_notes.json")
        let listenAlongBytes = Data("{\"schema\":\"shengjian.listen_along_notes.v1\",\"schema_version\":1,\"updated_at\":\"\",\"notes\":[],\"ext\":true}\n".utf8)
        try listenAlongBytes.write(to: listenAlong)
        let (storeA, defaultsA, suiteA) = try isolatedStore(documents)
        let (storeB, defaultsB, suiteB) = try isolatedStore(documents)
        defer {
            defaultsA.removePersistentDomain(forName: suiteA)
            defaultsB.removePersistentDomain(forName: suiteB)
        }
        let first = try stagingPackage(id: "ep-a", title: "From A", audio: "a-audio", extras: "a",
                                       extra: ["shelf_pin": "phone-a"])
        try writeStaging(documents: documents, txId: "tx-a", packages: [first])
        _ = try storeA.consumePushStaging()
        let digestA = LibrarySnapshot.digest(try Data(contentsOf: libraryListing(documents)))
        var stale = try JSONSerialization.jsonObject(with: try Data(contentsOf: libraryListing(documents))) as! [String: Any]
        var rows = stale["episodes"] as! [[String: Any]]
        rows[0]["title"] = "Stale overwrite"
        stale["episodes"] = rows
        do {
            _ = try storeB.commitLocalSnapshot(try json(stale), expecting: "0" + String(digestA.dropFirst()))
            throw Failure(description: "stale digest unexpectedly committed")
        } catch LibrarySnapshotError.staleCatalog {
            print("PASS competing commit with a stale CAS token is rejected")
        }
        let second = try stagingPackage(id: "ep-b", title: "From B", audio: "b-audio", extras: "b")
        try writeStaging(documents: documents, txId: "tx-b", packages: [second])
        let receipts = try storeB.consumePushStaging()
        try expect(receipts.contains(where: { $0.txId == "tx-b" && !$0.rebuilt }),
                   "second store ingests a distinct tx")
        storeA.reload()
        try expect(Set(storeA.episodes.map(\.id)) == Set(["ep-a", "ep-b"]),
                   "later ingest re-reads the live catalog and does not drop the first episode")
        try expect(Set(storeB.episodes.map(\.id)) == Set(["ep-a", "ep-b"]),
                   "both stores see the same unique catalog after the second commit")
        let snapshot = try LibrarySnapshot(Data(contentsOf: libraryListing(documents)))
        let rowA = snapshot.rows.first { ($0["id"] as? String) == "ep-a" }
        try expect(rowA?["shelf_pin"] as? String == "phone-a",
                   "phone-unique field survives a later ingest merge")
        try expect(snapshot.transactions["tx-a"] != nil && snapshot.transactions["tx-b"] != nil,
                   "ledger retains both transaction records")
        try expect(Data(contentsOf: listenAlong) == listenAlongBytes,
                   "serial ingest merges never overwrite listen_along_notes.json")
    }

    private static func ingestLostReceiptRetry(in root: URL) throws {
        let documents = root.appendingPathComponent("lost-receipt", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = try stagingPackage(id: "ep-first", title: "First", audio: "first", extras: "1")
        try writeStaging(documents: documents, txId: "tx-first", packages: [first])
        let original = try store.consumePushStaging()
        try expect(original.count == 1 && !original[0].rebuilt, "first tx commits and writes a receipt")
        let receiptURL = documents.appendingPathComponent(".push-staging/tx-first/receipt.json")
        try FileManager.default.removeItem(at: receiptURL)
        let second = try stagingPackage(id: "ep-second", title: "Second", audio: "second", extras: "2")
        try writeStaging(documents: documents, txId: "tx-second", packages: [second])
        let later = try store.consumePushStaging()
        try expect(later.contains(where: { $0.txId == "tx-second" && !$0.rebuilt }),
                   "a later tx still commits after the first receipt is lost")
        try expect(later.contains(where: { $0.txId == "tx-first" && $0.rebuilt && $0.resultDigest == original[0].resultDigest && $0.generation == original[0].generation }),
                   "retry of the original tx after a later commit is idempotent from the ledger")
        try expect(Set(store.episodes.map(\.id)) == Set(["ep-first", "ep-second"]),
                   "idempotent retry does not re-apply or drop either episode")
        let conflict = try stagingPackage(id: "ep-first", title: "Conflict", audio: "conflict", extras: "x",
                                          previous: "objects/\(first.revision)")
        try writeStaging(documents: documents, txId: "tx-first", packages: [conflict])
        do {
            _ = try store.consumePushStaging()
            throw Failure(description: "same tx with a different digest unexpectedly succeeded")
        } catch LibrarySnapshotError.transactionConflict {
            print("PASS same tx id with a different request digest is rejected")
        }
        try expect(store.episodes.first(where: { $0.id == "ep-first" })?.title == "First",
                   "rejected digest conflict leaves the original first episode in place")
    }

    private static func ingestFailClosed(in root: URL) throws {
        let documents = root.appendingPathComponent("fail-closed", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let seed = try stagingPackage(id: "ep-seed", title: "Seed", audio: "seed", extras: "s")
        try writeStaging(documents: documents, txId: "tx-seed", packages: [seed])
        _ = try store.consumePushStaging()
        let before = try Data(contentsOf: libraryListing(documents))

        try clearStaging(documents)
        let missing = try stagingPackage(id: "ep-miss", title: "Missing", audio: "miss", extras: "m")
        try writeStaging(documents: documents, txId: "tx-miss", packages: [missing])
        let missingFile = documents.appendingPathComponent(".push-staging/tx-miss/objects/\(missing.revision)/document.json")
        try FileManager.default.removeItem(at: missingFile)
        try ingestRefuses(store, "missing resource fails closed")
        try expect(Data(contentsOf: libraryListing(documents)) == before, "missing resource does not replace the catalog")

        try clearStaging(documents)
        let badHash = try stagingPackage(id: "ep-hash", title: "Bad Hash", audio: "hash", extras: "h")
        try writeStaging(documents: documents, txId: "tx-hash", packages: [badHash])
        let hashed = documents.appendingPathComponent(".push-staging/tx-hash/objects/\(badHash.revision)/output.mp3")
        try Data("tampered".utf8).write(to: hashed)
        try ingestRefuses(store, "hash mismatch fails closed")
        try expect(Data(contentsOf: libraryListing(documents)) == before, "hash mismatch does not replace the catalog")

        try clearStaging(documents)
        let full = try stagingPackage(id: "ep-full", title: "Disk Full", audio: "full", extras: "f")
        try writeStaging(documents: documents, txId: "tx-full", packages: [full])
        try ingestRefuses(store, fault: .diskFull, "disk-full fault fails closed before the unique commit")
        try expect(Data(contentsOf: libraryListing(documents)) == before, "disk-full fault leaves the old catalog")

        try clearStaging(documents)
        let linked = try stagingPackage(id: "ep-link", title: "Hardlink", audio: "link", extras: "l")
        try writeStaging(documents: documents, txId: "tx-link", packages: [linked])
        let target = documents.appendingPathComponent(".push-staging/tx-link/objects/\(linked.revision)/output.mp3")
        let sibling = documents.appendingPathComponent(".push-staging/tx-link/outside-bytes")
        try Data("link".utf8).write(to: sibling)
        try FileManager.default.removeItem(at: target)
        try FileManager.default.linkItem(at: sibling, to: target)
        try ingestRefuses(store, "hard-linked staging payload is refused")
        try expect(Data(contentsOf: libraryListing(documents)) == before, "hard-link refusal leaves the old catalog")

        try clearStaging(documents)
        let hidden = try stagingPackage(id: "ep-hidden", title: "Hidden", audio: "hidden", extras: "z")
        try writeStaging(documents: documents, txId: "tx-hidden", packages: [hidden])
        try FileManager.default.removeItem(at: documents.appendingPathComponent(".push-staging/tx-hidden/ready"))
        let ignored = try store.consumePushStaging()
        try expect(ignored.isEmpty && !store.episodes.contains(where: { $0.id == "ep-hidden" }),
                   "staging without the ready marker is not consumed")
        try expect(Data(contentsOf: libraryListing(documents)) == before, "unreadied staging does not touch the catalog")

        let stale = try stagingPackage(id: "ep-seed", title: "Stale", audio: "stale", extras: "s",
                                       previous: "episodes/ep-seed")
        let next = try stagingPackage(id: "ep-next", title: "Next", audio: "next", extras: "n")
        try writeStaging(documents: documents, txId: "tx-a-stale", packages: [stale])
        try writeStaging(documents: documents, txId: "tx-z-next", packages: [next])
        try ingestRefuses(store, "stale predecessor is reported")
        try expect(Set(store.episodes.map(\.title)) == Set(["Seed", "Next"]),
                   "stale staging keeps the old episode and does not block a later transaction")
        let snapshot = try LibrarySnapshot(Data(contentsOf: libraryListing(documents)))
        try expect(snapshot.transactions["tx-a-stale"] == nil && snapshot.transactions["tx-z-next"] != nil,
                   "only the valid queued push gets a transaction record")
    }

    private static func migrateLegacyInterruptRetry(in root: URL) throws {
        let fm = FileManager.default
        let documents = root.appendingPathComponent("migrate", isDirectory: true)
        let library = documents.appendingPathComponent("中文播客私人库", isDirectory: true)
        let listing = libraryListing(documents)
        let covers = ["legacy-png": "cover.png", "legacy-jpg": "cover.jpg", "legacy-webp": "cover.webp"]
        let legacyIDs = ["legacy-png", "legacy-jpg", "legacy-webp", "legacy-plain"]
        let consumedID = "fd4c6200b01540d992b5a9e39784eb2e"
        var legacyFiles: [String: [String: Data]] = [:]
        var rows: [[String: Any]] = []
        for id in legacyIDs {
            let folder = library.appendingPathComponent("episodes/\(id)", isDirectory: true)
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            var files = ["output.mp3": Data("synthetic audio \(id)".utf8)]
            var row: [String: Any] = [
                "id": id, "title": "Synthetic \(id)", "file": "output.mp3", "duration_sec": 90,
                "shelf_pin": "fixture", "handmade_tag": ["keep": id],
            ]
            if let cover = covers[id] {
                row["artwork"] = cover
                row["artwork_kind"] = "provided"
                files[cover] = Data("synthetic \(cover)".utf8)
            }
            if id == "legacy-png" {
                files["document.json"] = try json(["body": "synthetic transcript"])
                files["source-identity.json"] = try json(["id": id])
                files["render-identity.json"] = try json(["version": 1])
                files["episode.json"] = try json(["id": id])
            }
            for (name, data) in files { try data.write(to: folder.appendingPathComponent(name)) }
            legacyFiles[id] = files
            rows.append(row)
        }
        try json(["episodes": rows, "phone_settings": ["enabled": true]]).write(to: listing)
        let notes = documents.appendingPathComponent("listen_notes.json")
        let notesBytes = Data("{\n  \"synthetic\": \"fixture note\"\n}\n".utf8)
        try notesBytes.write(to: notes)
        let listenAlong = documents.appendingPathComponent("listen_along_notes.json")
        let listenAlongBytes = Data("{\"schema\":\"shengjian.listen_along_notes.v1\",\"schema_version\":1,\"updated_at\":\"\",\"notes\":[],\"extra_ext\":true}\n".utf8)
        try listenAlongBytes.write(to: listenAlong)

        let (store, defaults, suite) = try isolatedStore(documents)
        defer { defaults.removePersistentDomain(forName: suite) }
        let consumed = try stagingPackage(id: consumedID, title: "Synthetic consumed", audio: "consumed-audio",
                                          extras: "synthetic document", extra: ["artwork": "cover.png", "artwork_kind": "title"],
                                          additionalFiles: ["cover.png": Data("synthetic consumed cover".utf8)])
        try writeStaging(documents: documents, txId: "fixture-consumed", packages: [consumed])
        let consumedReceipt = try store.consumePushStaging().first
        let pushed = try LibrarySnapshot(Data(contentsOf: listing))
        try expect(consumedReceipt?.generation == 1 && pushed.transactions["fixture-consumed"] != nil,
                   "fixture starts after real staging consumption at generation 1")
        // Put the consumed row between legacy rows so appending it during migration would change order.
        var mixed = pushed.object
        mixed["episodes"] = [pushed.rows[0], pushed.rows[4]] + Array(pushed.rows[1..<4])
        let before = try json(mixed)
        try before.write(to: listing)
        let orderedIDs = [legacyIDs[0], consumedID] + Array(legacyIDs.dropFirst())
        let baseline = try LibrarySnapshot(before)
        store.reload()
        guard let legacy = store.episodes.first(where: { $0.id == legacyIDs[0] }),
              let existing = store.episodes.first(where: { $0.id == consumedID }),
              let cover = legacy.artworkURL, let existingCover = existing.artworkURL else {
            throw Failure(description: "mixed catalog did not resolve before migration")
        }
        _ = store.remember(episode: legacy, seconds: 42, duration: 90, finished: false)
        _ = store.remember(episode: existing, seconds: 3.5, duration: 12, finished: false)
        let progressKey = "PrivateListen.progress"
        let progressBefore = defaults.data(forKey: progressKey)
        let settingsKey = "PrivateListen.speed"
        defaults.set("1.25", forKey: settingsKey)
        let backup = library.appendingPathComponent(".migration-backup/library.json")

        func expectLegacyFilesUnchanged() throws {
            for (id, files) in legacyFiles {
                for (name, data) in files {
                    let url = library.appendingPathComponent("episodes/\(id)/\(name)")
                    guard try Data(contentsOf: url) == data else {
                        throw Failure(description: "legacy \(id)/\(name) changed")
                    }
                }
            }
        }

        for fault in [IngestFault.midInstall, .beforeCommit, .diskFull] {
            do {
                _ = try store.migrateLegacyLibrary(fault: fault)
                throw Failure(description: "\(fault) migration unexpectedly succeeded")
            } catch LibrarySnapshotError.rejectedIngest {
                print("PASS \(fault) interrupts migration before commit")
            }
            store.reload()
            try expect(Data(contentsOf: listing) == before && Data(contentsOf: backup) == before,
                       "\(fault) leaves the exact mixed catalog and its backup")
            try expect(store.episodes.contains(legacy) && store.episodes.contains(existing),
                       "\(fault) keeps the visible legacy and consumed media")
            try expect(defaults.data(forKey: progressKey) == progressBefore
                       && store.resumeSeconds(for: legacy) == 42 && store.resumeSeconds(for: existing) == 3.5,
                       "\(fault) preserves both progress marks")
            try expect(Data(contentsOf: notes) == notesBytes
                       && Data(contentsOf: listenAlong) == listenAlongBytes
                       && defaults.string(forKey: settingsKey) == "1.25",
                       "\(fault) preserves notes, listen-along notes, and settings")
            try expectLegacyFilesUnchanged()
        }

        let coverBytes = try Data(contentsOf: cover)
        try fm.removeItem(at: cover)
        do {
            _ = try store.migrateLegacyLibrary()
            throw Failure(description: "migration accepted a missing declared cover")
        } catch LibrarySnapshotError.incompleteRevision {
            try expect(Data(contentsOf: listing) == before, "missing cover leaves the old catalog")
        }
        try fm.createSymbolicLink(at: cover, withDestinationURL: existingCover)
        do {
            _ = try store.migrateLegacyLibrary()
            throw Failure(description: "migration accepted a symlinked declared cover")
        } catch LibrarySnapshotError.invalidPath {
            try expect(Data(contentsOf: listing) == before, "symlinked cover leaves the old catalog")
        }
        try fm.removeItem(at: cover)
        try coverBytes.write(to: cover)

        let receipt: IngestReceipt?
        do {
            receipt = try store.migrateLegacyLibrary()
        } catch {
            throw Failure(description: "default mixed legacy artwork migration after retry: \(error)")
        }
        let committed = try Data(contentsOf: listing)
        let snapshot = try LibrarySnapshot(committed)
        try expect(receipt?.txId == "migrate-legacy" && receipt?.generation == 2 && receipt?.rebuilt == false,
                   "retry commits the one-time legacy migration")
        let migration = snapshot.transactions["migrate-legacy"]
        try expect(snapshot.generation == 2 && snapshot.parentDigest == LibrarySnapshot.digest(before)
                   && migration?.requestDigest == LibrarySnapshot.digest(before)
                   && (try migration?.resultDigest == snapshot.bodyDigest())
                   && migration?.resultDigest == receipt?.resultDigest
                   && snapshot.transactions["fixture-consumed"] == baseline.transactions["fixture-consumed"],
                   "migration stamps generation, parent and ledger while retaining the consume receipt")
        try expect(snapshot.records.compactMap(\.catalogID) == orderedIDs,
                   "migration retains mixed catalog row order")
        try expect((snapshot.object["phone_settings"] as? [String: Bool])?["enabled"] == true,
                   "migration retains catalog settings")
        let loaded = LibraryStore.loadEpisodes(from: library, localCopy: true)
        try expect(loaded.accepted && loaded.episodes.count == orderedIDs.count,
                   "the committed mixed snapshot resolves from disk")
        for (index, id) in orderedIDs.enumerated() {
            guard let episode = loaded.episodes.first(where: { $0.id == id }),
                  let revision = snapshot.records[index].mediaRevision else {
                throw Failure(description: "resolved revision missing for \(id)")
            }
            var expectedRow = baseline.rows[index]
            var actualRow = snapshot.rows[index]
            let oldRevision = expectedRow.removeValue(forKey: "media_revision") as? [String: Any]
            let newRevision = actualRow.removeValue(forKey: "media_revision") as? [String: Any]
            try expect(json(actualRow) == json(expectedRow), "\(id) retains all row fields")
            let expectedFiles: [String: Data]
            if id == consumedID {
                try expect(json(newRevision ?? [:]) == json(oldRevision ?? [:]), "consumed revision is unchanged")
                expectedFiles = consumed.files
            } else {
                try expect(revision.path == "objects/\(revision.sha256)" && revision.previousPath == "episodes/\(id)",
                           "\(id) points to its immutable object and prior legacy directory")
                let fallback = Dictionary(uniqueKeysWithValues: ["document.json", "source-identity.json",
                    "render-identity.json", "episode.json"].map { ($0, Data("{}".utf8)) })
                expectedFiles = fallback.merging(legacyFiles[id] ?? [:]) { _, original in original }
                try expect(episode.artworkURL?.lastPathComponent == covers[id], "\(id) resolves its declared cover")
            }
            try expect(revision.files == expectedFiles.mapValues(LibrarySnapshot.digest),
                       "\(id) manifest binds every expected payload by SHA-256")
            for (name, expected) in expectedFiles {
                guard let url = episode.publishedAsset(name), try Data(contentsOf: url) == expected else {
                    throw Failure(description: "migrated bytes differ for \(id)/\(name)")
                }
            }
        }
        let reloaded = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: documents)
        reloaded.reload()
        try expect(reloaded.resumeSeconds(for: legacy.id) == 42 && reloaded.resumeSeconds(for: existing) == 3.5,
                   "legacy and consumed progress survive a fresh store load")
        try expect(Data(contentsOf: notes) == notesBytes
                   && Data(contentsOf: listenAlong) == listenAlongBytes
                   && defaults.string(forKey: settingsKey) == "1.25",
                   "migration preserves notes, listen-along notes, and settings")
        try expectLegacyFilesUnchanged()
        try expect(try store.migrateLegacyLibrary() == nil && Data(contentsOf: listing) == committed
                   && Data(contentsOf: backup) == before,
                   "completed migration is a no-op on retry and keeps the original backup")

        try shortSampleProbeLogic(in: library, store: reloaded, defaults: defaults, expecting: existing)
    }

    static func migrateLegacyDeviceShape(fixture: URL) throws {
        let fm = FileManager.default
        let source = fixture.appendingPathComponent("Documents", isDirectory: true)
        let documents = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fm.copyItem(at: source, to: documents)
        let (store, defaults, suite) = try isolatedStore(documents)
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? fm.removeItem(at: documents)
        }
        let listing = libraryListing(documents)
        let library = listing.deletingLastPathComponent()
        let objects = library.appendingPathComponent("objects")
        let rawBytes = try Data(contentsOf: listing)
        let raw = try LibrarySnapshot(rawBytes)
        let structure = try fixtureObject(fixture.appendingPathComponent("STRUCT.json"))
        let ids = raw.records.compactMap(\.catalogID)
        let consumedTx = "20261121T120217452138Z-01966c0f96b7493e9410751b2432eaeb"
        try expect(ids.count == 59 && ids == structure["ids_in_order"] as? [String]
                   && raw.generation == 1 && raw.object["schema"] as? String == PushStaging.schema
                   && raw.transactions.count == 1 && raw.transactions[consumedTx] != nil
                   && LibrarySnapshot.digest(rawBytes) == structure["catalog_sha256_redacted_compact"] as? String,
                   "device-shaped fixture has all 59 original rows and the consumed transaction")
        let pendingID = "mostprise-69320d8b4ef12f9fe9f8e1fe"
        let consumedID = "fd4c6200b01540d992b5a9e39784eb2e"
        guard let pendingIndex = ids.firstIndex(of: pendingID), let consumedIndex = ids.firstIndex(of: consumedID),
              let consumed = raw.records[consumedIndex].mediaRevision else {
            throw Failure(description: "device-shaped fixture is missing its pending or consumed package")
        }
        let pending = library.appendingPathComponent("episodes/\(pendingID)")
        let pendingFiles = try fm.contentsOfDirectory(atPath: pending.path).sorted()
        let coverRows = structure["covers"] as? [[String: Any]]
        try expect(pendingFiles == coverRows?[pendingIndex]["all_file_names"] as? [String] && pendingFiles.count == 10,
                   "pending fixture keeps all ten original file names")
        let original = try stagingPackage(id: "old-coverless-residue", title: "Unreferenced", audio: "stub", extras: "stub")
        let residue = objects.appendingPathComponent(original.revision)
        try fm.createDirectory(at: residue, withIntermediateDirectories: true)
        for (name, bytes) in original.files { try bytes.write(to: residue.appendingPathComponent(name)) }

        let rawFailure: Error
        do {
            _ = try store.migrateLegacyLibrary()
            throw Failure(description: "redacted pending package unexpectedly committed")
        } catch let error as Failure {
            throw error
        } catch {
            rawFailure = error
        }
        let retryObjects = Set(try fm.contentsOfDirectory(atPath: objects.path))
        try expect(Data(contentsOf: listing) == rawBytes && retryObjects.contains(original.revision),
                   "failed migration leaves the catalog and pre-existing coverless object in place")
        store.refreshLocalLibraryOnActivation()
        let migrationError = try fixtureObject(documents.appendingPathComponent(".library-activation-migrate-legacy-error.json"))
        let consumeError = try fixtureObject(documents.appendingPathComponent(".library-activation-consume-push-staging-error.json"))
        let nsError = rawFailure as NSError
        try expect(migrationError["operation"] as? String == "migrate-legacy"
                   && migrationError["error_case"] as? String == "invalidBinding"
                   && migrationError["domain"] as? String == nsError.domain
                   && migrationError["code"] as? Int == nsError.code
                   && migrationError["localized_description"] as? String == nsError.localizedDescription
                   && consumeError["operation"] as? String == "consume-push-staging"
                   && consumeError["error_case"] as? String == "incompleteRevision"
                   && Data(contentsOf: listing) == rawBytes,
                   "activation records both errors separately and keeps the old catalog")

        // Repair only the test copy's protocol literals and hashes broken by
        // redaction. Keep the 59 rows, stub audio, 675 segments and all file names.
        let boundBytes = try bindRedactedPendingFixture(raw, index: pendingIndex, folder: pending)
        try boundBytes.write(to: listing, options: .atomic)
        let bound = try LibrarySnapshot(boundBytes)
        guard let reference = bound.records[pendingIndex].pendingReference(for: pendingID) else {
            throw Failure(description: "the control is missing its pending reference")
        }
        _ = try PendingTranscript.read(id: pendingID, audioURL: pending.appendingPathComponent("output.mp3"),
                                       reference: reference).get()
        print("PASS 59-row control validates the pending manifest and hashes on the sanitized bytes before migration")
        let pendingBytes = try pendingFiles.map { try Data(contentsOf: pending.appendingPathComponent($0)) }

        let receipt = try store.migrateLegacyLibrary()
        let committedBytes = try Data(contentsOf: listing)
        let committed = try LibrarySnapshot(committedBytes)
        let ledger = committed.transactions["migrate-legacy"]
        try expect(receipt?.txId == "migrate-legacy" && receipt?.generation == 2
                   && ledger?.requestDigest == LibrarySnapshot.digest(boundBytes)
                   && ledger?.resultDigest == committed.bodyDigest() && ledger?.resultDigest == receipt?.resultDigest
                   && committed.parentDigest == LibrarySnapshot.digest(boundBytes)
                   && committed.transactions[consumedTx] == raw.transactions[consumedTx],
                   "retry commits generation 2 and keeps the existing consumed transaction")
        try expect(rawFailure as? PendingTranscript.ReadFailure == .invalidBinding,
                   "the exact redacted fixture still fails pending binding validation")
        try expect(committed.records.compactMap(\.catalogID) == ids && store.episodes.count == 59,
                   "all 59 catalog rows stay in order and resolve")
        var coverKinds: [String: Int] = [:]
        for (index, id) in ids.enumerated() {
            var row = committed.rows[index]
            if id == pendingID || id == consumedID {
                guard try json(row) == json(bound.rows[index]) else {
                    throw Failure(description: "pending or already-consumed row changed")
                }
                continue
            }
            row.removeValue(forKey: "media_revision")
            let record = committed.records[index]
            guard try json(row) == json(raw.rows[index]),
                  let revision = record.mediaRevision, revision.path == "objects/\(revision.sha256)",
                  revision.previousPath == "episodes/\(id)",
                  let cover = record.artwork, let kind = record.artworkKind, let sha = revision.files[cover] else {
                throw Failure(description: "legacy extensions, previous path, or declared cover changed")
            }
            let oldCover = try Data(contentsOf: source.appendingPathComponent("中文播客私人库/episodes/\(id)/\(cover)"))
            let newCover = try Data(contentsOf: library.appendingPathComponent("\(revision.path)/\(cover)"))
            guard oldCover == newCover, LibrarySnapshot.digest(oldCover) == sha else {
                throw Failure(description: "migrated cover bytes or manifest hash differ")
            }
            coverKinds["\(cover)|\(kind)", default: 0] += 1
        }
        try expect(coverKinds == ["cover.png|placeholder": 27, "cover.jpg|thumbnail": 26, "cover.webp|thumbnail": 4],
                   "57 covers preserve their original bytes, manifest hashes and artwork kinds")
        var unchangedPaths = ["listen_notes.json", "book_notes.v1.json", "book_state.v1.json", "library.json"]
            + ["ready", "manifest.json", "receipt.json"].map { ".push-staging/\(consumedTx)/\($0)" }
            + consumed.files.keys.map { "中文播客私人库/\(consumed.path)/\($0)" }
        if fm.fileExists(atPath: source.appendingPathComponent("listen_along_notes.json").path) {
            unchangedPaths.append("listen_along_notes.json")
        } else {
            try expect(!fm.fileExists(atPath: documents.appendingPathComponent("listen_along_notes.json").path),
                       "migration does not synthesize absent listen_along_notes.json")
        }
        try expect(unchangedPaths.allSatisfy { path in
            try Data(contentsOf: source.appendingPathComponent(path)) == Data(contentsOf: documents.appendingPathComponent(path))
        }, "notes, progress, staging sidecars, and the six consumed object files retain their bytes")
        let afterObjects = Set(try fm.contentsOfDirectory(atPath: objects.path))
        try expect(afterObjects == retryObjects && afterObjects.count == 59 && original.files.allSatisfy { name, bytes in
            try Data(contentsOf: residue.appendingPathComponent(name)) == bytes
        }, "retry reuses the 57 objects from the failed attempt and preserves older residue")
        try expect(PendingTranscript.read(id: pendingID, audioURL: pending.appendingPathComponent("output.mp3"),
                                          reference: reference).get().segments.count == 675
                   && fm.contentsOfDirectory(atPath: pending.path).sorted() == pendingFiles
                   && pendingFiles.map { try Data(contentsOf: pending.appendingPathComponent($0)) } == pendingBytes,
                   "pending package still validates at its legacy path after migration")
        try expect(try store.migrateLegacyLibrary() == nil && Data(contentsOf: listing) == committedBytes,
                   "a catalog with only pending legacy rows is a no-op on subsequent activation")
    }

    private static func fixtureObject(_ url: URL) throws -> [String: Any] {
        guard let object = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any] else {
            throw Failure(description: "fixture JSON object expected")
        }
        return object
    }

    private static func bindRedactedPendingFixture(_ snapshot: LibrarySnapshot, index: Int, folder: URL) throws -> Data {
        let recordURL = folder.appendingPathComponent("episode.json")
        let transcriptURL = folder.appendingPathComponent("transcript.pending.json")
        let manifestURL = folder.appendingPathComponent("manifest.json")
        var record = try fixtureObject(recordURL)
        var transcript = try fixtureObject(transcriptURL)
        var manifest = try fixtureObject(manifestURL)
        let input = "mostprise-original.m4a"
        let output = "output.mp3"
        let rawASR = "mostprise-original-asr-raw.json"
        guard let id = snapshot.records[index].catalogID, let declared = manifest["sha256"] as? [String: String],
              var derivation = manifest["derivation"] as? [String: Any] else {
            throw Failure(description: "redacted fixture missing pending manifest shape")
        }
        func hash(_ name: String) throws -> String {
            try LibrarySnapshot.digest(Data(contentsOf: LibrarySnapshot.path(name, in: folder)))
        }
        record["id"] = id
        record["file"] = output
        record["manifest"] = manifestURL.lastPathComponent
        record["transcript"] = transcriptURL.lastPathComponent
        transcript["episode_id"] = id
        transcript["timeline"] = "output.mp3.seconds"
        transcript["source_audio_sha256"] = try hash(input)
        transcript["playback_audio_sha256"] = try hash(output)
        transcript["raw_asr_sha256"] = try hash(rawASR)
        try json(record).write(to: recordURL)
        try json(transcript).write(to: transcriptURL)
        derivation["input"] = input
        derivation["output"] = output
        derivation["input_sha256"] = transcript["source_audio_sha256"]
        derivation["output_sha256"] = transcript["playback_audio_sha256"]
        manifest["derivation"] = derivation
        manifest["episode_id"] = id
        manifest["sha256"] = try Dictionary(uniqueKeysWithValues: declared.keys.map { name in (name, try hash(name)) })
        try json(manifest).write(to: manifestURL)
        var object = snapshot.object
        var rows = snapshot.rows
        rows[index]["transcript"] = transcriptURL.lastPathComponent
        object["episodes"] = rows
        return try json(object)
    }

    private static func shortSampleProbeLogic(in root: URL, store: LibraryStore, defaults: UserDefaults,
                                              expecting existing: Episode) throws {
        guard let target = NortonDeviceFourProbe.target(in: store, root: root), target == existing else {
            throw Failure(description: "probe did not select the approved short sample")
        }
        try expect(target.publishedAsset("extract.json") == nil && target.publishedAsset("point-listen.json") == nil,
                   "probe selects the approved object with only main-track files in a mixed library")
        try expect(NortonDeviceFourProbe.target(in: store, root: root.appendingPathComponent("other")) == nil,
                   "probe refuses a different library root")
        _ = store.remember(episode: target, seconds: 4.2, duration: 12, finished: false)
        try expect(NortonDeviceFourProbe.checkpointMatches(target, in: store, defaults: defaults,
                                                          playhead: 4.2, wasFinished: false),
                   "probe assertion resolves the under-five-second Continue and revision-keyed mark")
        try expect(!NortonDeviceFourProbe.checkpointMatches(target, in: store, defaults: defaults,
                                                           playhead: 7, wasFinished: false),
                   "probe assertion refuses a playhead that differs from the checkpoint")
        store.dismissContinue(target.id)
        try expect(!NortonDeviceFourProbe.checkpointMatches(target, in: store, defaults: defaults,
                                                           playhead: 4.2, wasFinished: false),
                   "probe assertion refuses a dismissed Continue entry")
        store.acknowledgeContinue(target)
        store.markFinished(target.id)
        _ = store.remember(episode: target, seconds: 4.2, duration: 12, finished: false)
        try expect(NortonDeviceFourProbe.checkpointMatches(target, in: store, defaults: defaults,
                                                          playhead: 4.2, wasFinished: true),
                   "probe assertion accepts a replay cursor with the prior finish preserved")
    }

    private static func clearStaging(_ documents: URL) throws {
        let fm = FileManager.default
        for name in [PushStaging.stagingName, PushStaging.privateName] {
            let url = documents.appendingPathComponent(name, isDirectory: true)
            if fm.fileExists(atPath: url.path) {
                try fm.removeItem(at: url)
            }
        }
    }

    private static func isolatedStore(_ documents: URL) throws -> (LibraryStore, UserDefaults, String) {
        let suite = "PrivateListen.ingest.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else {
            throw Failure(description: "defaults suite")
        }
        defaults.removePersistentDomain(forName: suite)
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: documents)
        return (store, defaults, suite)
    }

    private static func libraryListing(_ documents: URL) -> URL {
        documents.appendingPathComponent("中文播客私人库/library.json")
    }

    private static func ingestRefuses(_ store: LibraryStore, fault: IngestFault = .none, _ message: String) throws {
        do {
            _ = try store.consumePushStaging(fault: fault)
            throw Failure(description: "\(message): ingest unexpectedly succeeded")
        } catch let failure as Failure {
            throw failure
        } catch LibrarySnapshotError.rejectedIngest, LibrarySnapshotError.incompleteRevision,
                LibrarySnapshotError.invalidCatalog, LibrarySnapshotError.invalidPath,
                LibrarySnapshotError.transactionConflict, LibrarySnapshotError.staleCatalog {
            print("PASS \(message)")
        } catch {
            throw Failure(description: "\(message): unexpected error \(error)")
        }
    }

    private struct StagingPackage {
        var id: String
        var revision: String
        var package: [String: Any]
        var files: [String: Data]
    }

    private static func stagingPackage(id: String, title: String, audio: String, extras: String,
                                       previous: String = "", extra: [String: Any] = [:],
                                       additionalFiles: [String: Data] = [:]) throws -> StagingPackage {
        let files: [String: Data] = [
            "output.mp3": Data(audio.utf8),
            "document.json": Data("{\"body\":\"\(extras)\"}".utf8),
            "source-identity.json": Data("{}".utf8),
            "render-identity.json": Data("{}".utf8),
            "episode.json": Data("{\"id\":\"\(id)\"}".utf8),
        ].merging(additionalFiles) { _, additional in additional }
        let hashes = Dictionary(uniqueKeysWithValues: files.map { ($0.key, LibrarySnapshot.digest($0.value)) })
        let fileLines = hashes.sorted(by: { $0.key < $1.key }).map { "  \"\($0.key)\": \"\($0.value)\"" }
        let revision = LibrarySnapshot.digest(Data(("{\n" + fileLines.joined(separator: ",\n") + "\n}\n").utf8))
        var meta: [String: Any] = [:]
        for (name, data) in files {
            meta[name] = ["sha256": hashes[name]!, "size": data.count]
        }
        var row: [String: Any] = ["id": id, "title": title, "file": "output.mp3", "duration_sec": 12]
        for (key, value) in extra { row[key] = value }
        let package: [String: Any] = [
            "id": id,
            "revision": revision,
            "previous_path": previous,
            "files": meta,
            "row": row,
        ]
        return StagingPackage(id: id, revision: revision, package: package, files: files)
    }

    private static func writeStaging(documents: URL, txId: String, packages: [StagingPackage],
                                     requestDigest: String? = nil) throws {
        let fm = FileManager.default
        let root = documents.appendingPathComponent(".push-staging/\(txId)", isDirectory: true)
        if fm.fileExists(atPath: root.path) { try fm.removeItem(at: root) }
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        for item in packages {
            let folder = root.appendingPathComponent("objects/\(item.revision)", isDirectory: true)
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            for (name, data) in item.files {
                try data.write(to: folder.appendingPathComponent(name))
            }
        }
        let body = packages.map(\.package)
        let digest = try PushStaging.requestDigest(txId: txId, packages: body)
        let manifest: [String: Any] = [
            "tx_id": txId,
            "request_digest": requestDigest ?? digest,
            "packages": body,
        ]
        let data = try JSONSerialization.data(withJSONObject: manifest, options: [.sortedKeys])
        try data.write(to: root.appendingPathComponent(PushStaging.manifestName))
        try Data().write(to: root.appendingPathComponent(PushStaging.readyName))
    }

    static func runVerificationAndStreamingChecks() async throws {
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("private-listen-verify-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: scratch) }

        // 1. Cold start / reload must not hash the library on the main thread.
        do {
            let docs = scratch.appendingPathComponent("cold-start", isDirectory: true)
            try fm.createDirectory(at: docs, withIntermediateDirectories: true)
            let (seedStore, seedDefaults, seedSuite) = try isolatedStore(docs)
            defer { seedDefaults.removePersistentDomain(forName: seedSuite) }
            let packages = try (1...3).map { index in
                try stagingPackage(id: "ep-cold-\(index)", title: "Cold \(index)", audio: "cold-audio-\(index)", extras: "cold-\(index)")
            }
            try writeStaging(documents: docs, txId: "tx-cold", packages: packages)
            _ = try seedStore.consumePushStaging()

            let suite = "PrivateListen.cold.\(UUID().uuidString)"
            guard let defaults = UserDefaults(suiteName: suite) else { throw Failure(description: "cold defaults") }
            defer { defaults.removePersistentDomain(forName: suite) }
            let coldStore = LibraryStore(defaults: defaults, loadLibraryOnInit: true, documentsDirectory: docs)
            try expect(coldStore.hasLoadedOnce && coldStore.episodes.count == 3 && coldStore.episodes.allSatisfy(\.isPlayableNow),
                       "cold start synchronously presents all playable episodes")
            try expect(coldStore.verifierForTesting.hashComputeCount == 0 && !coldStore.verifierForTesting.hashedOnMainThread,
                       "cold start performs zero SHA hashing on main thread")
            await coldStore.waitForVerification()
            try expect(coldStore.verifierForTesting.hashComputeCount > 0 && !coldStore.verifierForTesting.hashedOnMainThread,
                       "background verification hashes off the main thread")

            // 4. Repeated reload() does not re-hash the full library.
            let countBefore = coldStore.verifierForTesting.hashComputeCount
            for _ in 0..<3 {
                coldStore.reload()
                await coldStore.waitForVerification()
            }
            try expect(coldStore.verifierForTesting.hashComputeCount == countBefore,
                       "repeated reload reuses positive stamp cache without re-hashing")
        }

        // 2. Same-length tampered bytes detected, negative cache prevents whitewash, and repair recovers.
        do {
            let docs = scratch.appendingPathComponent("same-len-tamper", isDirectory: true)
            try fm.createDirectory(at: docs, withIntermediateDirectories: true)
            let (store, defaults, suite) = try isolatedStore(docs)
            defer { defaults.removePersistentDomain(forName: suite) }
            let originalAudio = "1234567890"
            let tamperedAudio = Data("X234567890".utf8)
            let pkg = try stagingPackage(id: "ep-tamper", title: "Tamper", audio: originalAudio, extras: "t")
            try writeStaging(documents: docs, txId: "tx-tamper", packages: [pkg])
            _ = try store.consumePushStaging()
            guard let target = store.episodes.first else { throw Failure(description: "tamper seed") }

            let freshSuite = "PrivateListen.tamper.\(UUID().uuidString)"
            guard let freshDefaults = UserDefaults(suiteName: freshSuite) else { throw Failure(description: "tamper defaults") }
            defer { freshDefaults.removePersistentDomain(forName: freshSuite) }
            try tamperedAudio.write(to: target.audioURL, options: .atomic)
            let reader = LibraryStore(defaults: freshDefaults, loadLibraryOnInit: false, documentsDirectory: docs)
            reader.reload()
            try expect(reader.episodes.count == 1 && reader.verifierForTesting.hashComputeCount == 0,
                       "reload returns synchronously before hashing same-length tampered audio")
            await reader.waitForVerification()
            try expect(reader.episodes.first?.unavailableReason == .audio && reader.episodes.first?.isPlayableNow == false,
                       "background verification catches same-length wrong-byte audio")

            reader.reload()
            try expect(reader.episodes.first?.unavailableReason == .audio && reader.episodes.first?.isPlayableNow == false,
                       "negative stamp cache prevents subsequent lightweight reload from whitewashing bad episode")

            try Data(originalAudio.utf8).write(to: target.audioURL, options: .atomic)
            try fm.setAttributes([.modificationDate: Date().addingTimeInterval(2)], ofItemAtPath: target.audioURL.path)
            reader.reload()
            await reader.waitForVerification()
            try expect(reader.episodes.first?.unavailableReason == nil && reader.episodes.first?.isPlayableNow == true,
                       "repairing the audio invalidates negative stamp cache and restores playback")
        }

        // 3. On-demand verifyEpisodeNow intercepts bad audio and cancelled validation never writes .failed.
        do {
            let docs = scratch.appendingPathComponent("on-demand", isDirectory: true)
            try fm.createDirectory(at: docs, withIntermediateDirectories: true)
            let (seedStore, seedDefaults, seedSuite) = try isolatedStore(docs)
            defer { seedDefaults.removePersistentDomain(forName: seedSuite) }
            let pkg = try stagingPackage(id: "ep-ondemand", title: "On Demand", audio: "abcdefghij", extras: "od")
            try writeStaging(documents: docs, txId: "tx-od", packages: [pkg])
            _ = try seedStore.consumePushStaging()
            guard let seeded = seedStore.episodes.first else { throw Failure(description: "on-demand seed") }
            try Data("Zbcdefghij".utf8).write(to: seeded.audioURL, options: .atomic)

            let suite = "PrivateListen.ondemand.\(UUID().uuidString)"
            guard let defaults = UserDefaults(suiteName: suite) else { throw Failure(description: "ondemand defaults") }
            defer { defaults.removePersistentDomain(forName: suite) }
            let store = LibraryStore(defaults: defaults, loadLibraryOnInit: true, documentsDirectory: docs)
            guard let target = store.episodes.first else { throw Failure(description: "on-demand target") }
            let checked = await store.verifyEpisodeNow(target)
            try expect(checked.unavailableReason == .audio
                       && store.episodes.first?.unavailableReason == .audio
                       && !store.remember(episode: target, seconds: 12, duration: 60, finished: false)
                       && defaults.data(forKey: "PrivateListen.progress") == nil,
                       "verifyEpisodeNow blocks tampered audio, updates shelf, and rejects dirty progress")

            let validFile = scratch.appendingPathComponent("cancel-valid.bin")
            let validData = Data("cancel-check-bytes".utf8)
            try validData.write(to: validFile)
            let validDigest = LibrarySnapshot.digest(validData)
            let cancelVerifier = RevisionAssetVerifier()
            let cancelledTask = Task {
                try cancelVerifier.validate(validFile, digest: validDigest, checkCancellation: true)
            }
            cancelledTask.cancel()
            do {
                try await cancelledTask.value
            } catch is CancellationError {
                // Expected cancellation path.
            }
            try expect(cancelVerifier.lightweightStatus(validFile, digest: validDigest) != .failed,
                       "cancelled validate never writes .failed into cache")
            try cancelVerifier.validate(validFile, digest: validDigest, checkCancellation: false)
        }

        // 5. >1MiB multi-chunk (2.5MiB) streaming ingest & strict commit contract.
        do {
            let docs = scratch.appendingPathComponent("streaming-2m5", isDirectory: true)
            try fm.createDirectory(at: docs, withIntermediateDirectories: true)
            let (store, defaults, suite) = try isolatedStore(docs)
            defer { defaults.removePersistentDomain(forName: suite) }
            let size = 2 * 1_048_576 + 524_288 // 2.5 MiB
            var bigBytes = Data((0..<size).map { UInt8($0 & 0xff) })
            let bigString = String(decoding: bigBytes.map { ($0 % 26) + 65 }, as: UTF8.self)
            bigBytes = Data(bigString.utf8)
            let bigPkg = try stagingPackage(id: "ep-big", title: "Big Audio", audio: bigString, extras: "big")
            try writeStaging(documents: docs, txId: "tx-big-ok", packages: [bigPkg])
            let receipts = try store.consumePushStaging()
            try expect(receipts.count == 1 && store.episodes.count == 1 && store.episodes[0].isPlayableNow,
                       "2.5MiB multi-chunk staging package ingests cleanly")
            await store.waitForVerification()
            try expect(store.isEpisodeVerified(store.episodes[0]),
                       "2.5MiB episode passes multi-chunk background SHA")

            // Flip 1 byte in the 2nd MiB (same length) -> verifyPackages refuses and keeps library.json intact.
            let listing = libraryListing(docs)
            let beforeCatalog = try Data(contentsOf: listing)
            let badPkg = try stagingPackage(id: "ep-big-bad", title: "Big Bad", audio: bigString, extras: "bad")
            try writeStaging(documents: docs, txId: "tx-big-bad", packages: [badPkg])
            let stagedAudio = docs.appendingPathComponent(".push-staging/tx-big-bad/objects/\(badPkg.revision)/output.mp3")
            var tamperedBig = bigBytes
            tamperedBig[1_500_000] ^= 0x01
            try tamperedBig.write(to: stagedAudio)
            try ingestRefuses(store, "2.5MiB 2nd-MiB single-byte tamper rejected by streaming verifyPackages")
            try expect(Data(contentsOf: listing) == beforeCatalog,
                       "rejected 2.5MiB staging leaves library.json unchanged")

            // Tamper existing installed object at 2nd MiB -> installObject and commitLocalSnapshot both refuse.
            try clearStaging(docs)
            let existingPkg = try stagingPackage(id: "ep-big-existing", title: "Big Existing", audio: bigString, extras: "ex")
            let existingDir = docs.appendingPathComponent("中文播客私人库/objects/\(existingPkg.revision)", isDirectory: true)
            try fm.createDirectory(at: existingDir, withIntermediateDirectories: true)
            for (name, data) in existingPkg.files {
                try data.write(to: existingDir.appendingPathComponent(name))
            }
            try tamperedBig.write(to: existingDir.appendingPathComponent("output.mp3"))
            try writeStaging(documents: docs, txId: "tx-big-existing", packages: [existingPkg])
            try ingestRefuses(store, "2.5MiB existing object tampered at 2nd MiB rejected by installObject")

            let snapshot = try LibrarySnapshot(beforeCatalog)
            var badRows = snapshot.rows
            var badRow = badRows[0]
            var rev = badRow["media_revision"] as! [String: Any]
            rev["sha256"] = existingPkg.revision
            rev["path"] = "objects/\(existingPkg.revision)"
            rev["previous_path"] = ""
            rev["files"] = existingPkg.files.mapValues(LibrarySnapshot.digest)
            badRow["id"] = "ep-big-existing"
            badRow["media_revision"] = rev
            badRows.append(badRow)
            let proposed = try json(["episodes": badRows])
            try refuses { try store.commitLocalSnapshot(proposed, expecting: LibrarySnapshot.digest(beforeCatalog)) }
            try expect(Data(contentsOf: listing) == beforeCatalog,
                       "strict commitLocalSnapshot refuses 2.5MiB object tampered in 2nd MiB")
        }
    }

    private static func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    private static func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        guard try condition() else { throw Failure(description: message) }
        print("PASS \(message)")
    }

    private static func refuses(_ action: () throws -> String) throws {
        do {
            _ = try action()
        } catch {
            return
        }
        throw Failure(description: "unsafe snapshot commit unexpectedly succeeded")
    }
}
#endif
