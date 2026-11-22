import Foundation

@main
struct LibrarySnapshotProbe {
    @MainActor
    static func main() {
        do {
            var args = Array(CommandLine.arguments.dropFirst())
            if args.count == 2 && args[0] == "--publisher-only" {
                try verifyPublisherOutput(URL(fileURLWithPath: args[1], isDirectory: true))
                return
            }
            if args.count == 3 && args[0] == "--consume-staging" {
                try verifyStagedPublish(URL(fileURLWithPath: args[1], isDirectory: true), expecting: args[2])
                return
            }
            if args.count == 2 && args[0] == "--migration-fixture" {
                try LibrarySnapshotChecks.migrateLegacyDeviceShape(fixture: URL(fileURLWithPath: args[1], isDirectory: true))
                return
            }
            try LibrarySnapshotChecks.run()
            if let idx = args.firstIndex(of: "--adapter-documents"), args.indices.contains(idx + 1) {
                try LibrarySnapshotChecks.consumeAdapterDocuments(URL(fileURLWithPath: args[idx + 1], isDirectory: true))
                args.removeSubrange(idx...idx + 1)
            }
            if let idx = args.firstIndex(of: "--migration-fixture"), args.indices.contains(idx + 1) {
                try LibrarySnapshotChecks.migrateLegacyDeviceShape(fixture: URL(fileURLWithPath: args[idx + 1], isDirectory: true))
                args.removeSubrange(idx...idx + 1)
            }
            if args.count == 1 {
                try verifyPublisherOutput(URL(fileURLWithPath: args[0], isDirectory: true))
            }
        } catch {
            FileHandle.standardError.write(Data("FAIL \(error)\n".utf8))
            exit(1)
        }
    }

    @MainActor
    private static func verifyStagedPublish(_ documents: URL, expecting outcome: String) throws {
        let suite = "PrivateListen.staging-fixture.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { throw CocoaError(.fileReadUnknown) }
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: documents)
        store.reload()
        let listing = documents.appendingPathComponent("中文播客私人库/library.json")
        let before = try? Data(contentsOf: listing)
        let old = store.episodes.first
        let pinned = try old.map { try FileHandle(forReadingFrom: $0.audioURL) }
        defer { try? pinned?.close() }
        let oldPCM = try pinned.map(decodeMP3)
        if let old { store.remember(episode: old, seconds: 0.5, duration: 1, finished: false) }
        var result = "commit"
        do {
            let receipts = try store.consumePushStaging()
            if receipts.allSatisfy(\.rebuilt) { result = "ignored" }
        } catch let error as LibrarySnapshotError {
            result = String(describing: error)
        }
        guard result == outcome else {
            throw LibrarySnapshotChecks.Failure(description: "staging expected \(outcome), got \(result)")
        }
        if let pinned {
            try pinned.seek(toOffset: 0)
            guard try decodeMP3(pinned) == oldPCM else {
                throw LibrarySnapshotChecks.Failure(description: "pinned old audio changed")
            }
        }
        if outcome != "commit" {
            guard let old, try Data(contentsOf: listing) == before, store.episodes == [old],
                  store.resumeSeconds(for: old) == 0.5 else {
                throw LibrarySnapshotChecks.Failure(description: "refused staging changed the live catalog or media")
            }
            print("PASS \(outcome) preserves the live catalog, old PCM and playhead")
            return
        }
        guard store.episodes.count == 1, let episode = store.episodes.first,
              let transcript = episode.publishedAsset("document.json") else {
            throw LibrarySnapshotChecks.Failure(description: "staging did not publish the App episode and transcript")
        }
        let newFile = try FileHandle(forReadingFrom: episode.audioURL)
        defer { try? newFile.close() }
        let pcm = try decodeMP3(newFile)
        let version = old == nil ? 1 : 2
        guard !pcm.isEmpty, pcm != oldPCM,
              try String(contentsOf: transcript, encoding: .utf8).contains("第 \(version) 版译稿"),
              old == nil || (old?.id == episode.id && store.resumeSeconds(for: episode) == 0) else {
            throw LibrarySnapshotChecks.Failure(description: "staging did not switch to the matching audio and transcript")
        }
        print("PASS staged v\(version) committed with matching transcript and decodable PCM")
    }

    @MainActor
    private static func verifyPublisherOutput(_ documents: URL) throws {
        let suite = "PrivateListen.publisher-fixture.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { throw CocoaError(.fileReadUnknown) }
        defer { defaults.removePersistentDomain(forName: suite) }
        let root = documents.appendingPathComponent("中文播客私人库", isDirectory: true)
        let listing = root.appendingPathComponent("library.json")
        let newBytes = try Data(contentsOf: documents.appendingPathComponent("incoming.json"))
        let before = try Data(contentsOf: listing)
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false, documentsDirectory: documents)
        store.reload()
        guard let old = store.episodes.first, old.mediaRevision == nil else {
            throw LibrarySnapshotChecks.Failure(description: "publisher fixture legacy catalog")
        }
        let oldFile = try FileHandle(forReadingFrom: old.audioURL)
        defer { try? oldFile.close() }
        let reference = try FileHandle(forReadingFrom: old.audioURL)
        defer { try? reference.close() }
        let firstSamples = try decodeMP3(reference)
        store.remember(episode: old, seconds: 0.5, duration: 1, finished: false)
        try store.commitLocalSnapshot(newBytes, expecting: LibrarySnapshot.digest(before))
        guard let new = store.episodes.first, old.id == new.id, !old.matchesMedia(new),
              new.show == "Fixture show", new.lensIds == ["fixture"],
              store.resumeSeconds(for: old) == 0.5, store.resumeSeconds(for: new) == 0,
              let transcript = new.publishedAsset("document.json") else {
            throw LibrarySnapshotChecks.Failure(description: "publisher fixture revision and resume binding")
        }
        let newFile = try FileHandle(forReadingFrom: new.audioURL)
        defer { try? newFile.close() }
        let newSamples = try decodeMP3(newFile)
        let oldAfter = try decodeMP3(oldFile)
        let draft = try String(contentsOf: transcript, encoding: .utf8)
        guard !firstSamples.isEmpty, firstSamples == oldAfter, !newSamples.isEmpty,
              newSamples != firstSamples, draft.contains("第 2 版译稿") else {
            throw LibrarySnapshotChecks.Failure(description: "published audio, pinned old decoder and revision transcript")
        }
        print("PASS frozen publisher produced both catalogs and both valid MP3s")
        print("PASS App committed the real revision and opened its matching document.json")
        print("PASS the open old media handle still decodes to identical PCM, distinct from new media")
    }

    private static func decodeMP3(_ media: FileHandle) throws -> Data {
        let process = Process()
        let pcm = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["ffmpeg", "-v", "error", "-xerror", "-f", "mp3", "-i", "pipe:0",
                             "-map", "0:a:0", "-f", "s16le", "pipe:1"]
        process.standardInput = media
        process.standardOutput = pcm
        try process.run()
        let samples = pcm.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw LibrarySnapshotChecks.Failure(description: "fixture MP3 decode")
        }
        return samples
    }
}
