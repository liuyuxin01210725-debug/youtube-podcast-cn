import CryptoKit
import Foundation
import XCTest
@testable import PrivateListen

private final class InMemoryOSSSecretStore: OSSSecretStore, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: String] = [:]
    var failWrites = false

    private func key(_ service: String, _ account: String) -> String {
        "\(service)::\(account)"
    }

    func read(service: String, account: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return storage[key(service, account)]
    }

    func write(_ value: String, service: String, account: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if failWrites { return false }
        storage[key(service, account)] = value
        return true
    }

    func delete(service: String, account: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        storage.removeValue(forKey: key(service, account))
        return true
    }
}

private final class StubOSSURLProtocol: URLProtocol, @unchecked Sendable {
    struct RecordedRequest: Sendable {
        let url: URL
        let method: String
        let tokenHeader: String?
    }

    struct StubResponse: Sendable {
        let statusCode: Int
        let headers: [String: String]
        let body: Data
        let error: Error?

        init(statusCode: Int = 200, headers: [String: String] = [:], body: Data = Data(), error: Error? = nil) {
            self.statusCode = statusCode
            self.headers = headers
            self.body = body
            self.error = error
        }
    }

    private final class Registry: @unchecked Sendable {
        let lock = NSLock()
        var handlers: [String: @Sendable (URLRequest) -> StubResponse] = [:]
        var requestsByScope: [String: [RecordedRequest]] = [:]
    }

    private static let registry = Registry()

    static func register(scopeHost: String, handler: @escaping @Sendable (URLRequest) -> StubResponse) {
        registry.lock.lock()
        registry.handlers[scopeHost] = handler
        registry.requestsByScope[scopeHost] = []
        registry.lock.unlock()
    }

    static func unregister(scopeHost: String) {
        registry.lock.lock()
        registry.handlers.removeValue(forKey: scopeHost)
        registry.requestsByScope.removeValue(forKey: scopeHost)
        registry.lock.unlock()
    }

    static func requests(for scopeHost: String) -> [RecordedRequest] {
        registry.lock.lock()
        defer { registry.lock.unlock() }
        return registry.requestsByScope[scopeHost] ?? []
    }

    static func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubOSSURLProtocol.self]
        config.urlCache = nil
        config.httpCookieStorage = nil
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool {
        guard let host = request.url?.host else { return false }
        registry.lock.lock()
        let hasHandler = registry.handlers[host] != nil
        registry.lock.unlock()
        return hasHandler
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let url = request.url, let host = url.host else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let recorded = RecordedRequest(
            url: url,
            method: request.httpMethod ?? "GET",
            tokenHeader: request.value(forHTTPHeaderField: "X-Shengjian-Token")
        )

        Self.registry.lock.lock()
        Self.registry.requestsByScope[host, default: []].append(recorded)
        let handler = Self.registry.handlers[host]
        Self.registry.lock.unlock()

        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }

        let stub = handler(request)
        if let error = stub.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }

        let response = HTTPURLResponse(
            url: url,
            statusCode: stub.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: stub.headers
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !stub.body.isEmpty {
            client?.urlProtocol(self, didLoad: stub.body)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
final class OSSPullTests: XCTestCase {
    private struct TestFixture {
        let rootTemp: URL
        let suiteName: String
        let defaults: UserDefaults
        let secretStore: InMemoryOSSSecretStore
        let bridgeHost: String
        let ossHost: String

        var baseURLString: String { "https://\(bridgeHost)" }
        var libraryRoot: URL { rootTemp.appendingPathComponent("中文播客私人库", isDirectory: true) }

        func cleanup() {
            StubOSSURLProtocol.unregister(scopeHost: bridgeHost)
            StubOSSURLProtocol.unregister(scopeHost: ossHost)
            defaults.removePersistentDomain(forName: suiteName)
            try? FileManager.default.removeItem(at: rootTemp)
        }
    }

    private func makeFixture() throws -> TestFixture {
        let id = UUID().uuidString
        let rootTemp = FileManager.default.temporaryDirectory.appendingPathComponent("oss-test-\(id)", isDirectory: true)
        try FileManager.default.createDirectory(at: rootTemp, withIntermediateDirectories: true)
        let suiteName = "PrivateListen.oss-test.\(id)"
        let defaults = UserDefaults(suiteName: suiteName)!
        return TestFixture(
            rootTemp: rootTemp,
            suiteName: suiteName,
            defaults: defaults,
            secretStore: InMemoryOSSSecretStore(),
            bridgeHost: "bridge-\(id).test",
            ossHost: "oss-\(id).test"
        )
    }

    private func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func writeLocalEpisode(
        id: String,
        title: String,
        in libraryRoot: URL,
        extraRowFields: [String: Any] = [:]
    ) throws -> Data {
        let fm = FileManager.default
        try fm.createDirectory(at: libraryRoot, withIntermediateDirectories: true)

        let audio = Data("local-audio-\(id)".utf8)
        let emptyJSON = Data("{}".utf8)
        let files: [String: Data] = [
            "output.mp3": audio,
            "document.json": emptyJSON,
            "source-identity.json": emptyJSON,
            "render-identity.json": emptyJSON,
            "episode.json": emptyJSON,
        ]
        var hashes: [String: String] = [:]
        for (name, data) in files {
            hashes[name] = sha256Hex(data)
        }
        let entries = hashes.sorted { $0.key < $1.key }.map { "  \"\($0.key)\": \"\($0.value)\"" }
        let manifest = "{\n" + entries.joined(separator: ",\n") + "\n}\n"
        let revision = sha256Hex(Data(manifest.utf8))

        let objDir = libraryRoot.appendingPathComponent("objects/\(revision)", isDirectory: true)
        try fm.createDirectory(at: objDir, withIntermediateDirectories: true)
        for (name, data) in files {
            try data.write(to: objDir.appendingPathComponent(name), options: .atomic)
        }

        var row: [String: Any] = [
            "id": id,
            "title": title,
            "created_at": "2026-11-28T10:00:00Z",
            "duration_sec": 120.0,
            "file": "output.mp3",
            "media_revision": [
                "sha256": revision,
                "path": "objects/\(revision)",
                "previous_path": "",
                "resume_policy": "reset_unmapped",
                "files": hashes,
            ],
        ]
        for (k, v) in extraRowFields {
            row[k] = v
        }
        let catalog: [String: Any] = [
            "schema": PushStaging.schema,
            "generation": 1,
            "episodes": [row],
            "transactions": [:],
        ]
        let catalogData = try LibrarySnapshot.json(catalog)
        try catalogData.write(to: libraryRoot.appendingPathComponent("library.json"), options: .atomic)
        return catalogData
    }

    // 1. 未配置严格无操作
    func testUnconfiguredIsStrictNoOp() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        let beforeCatalog = try writeLocalEpisode(id: "ep-local", title: "Local Title", in: fx.libraryRoot)

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { _ in
            XCTFail("Unconfigured OSSConfig must never hit network")
            return StubOSSURLProtocol.StubResponse(statusCode: 500)
        }

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())

        let cases: [(String?, String?)] = [
            (nil, nil),
            (fx.baseURLString, nil),
            (nil, "secret-token"),
            ("file:///tmp/bad-scheme", "secret-token"),
            ("https://user:pass@\(fx.bridgeHost)", "secret-token"),
        ]

        for (rawURL, rawToken) in cases {
            fx.defaults.removeObject(forKey: OSSConfig.baseURLDefaultsKey)
            fx.defaults.removeObject(forKey: OSSConfig.legacyTokenDefaultsKey)
            _ = fx.secretStore.delete(service: OSSConfig.keychainService, account: OSSConfig.keychainAccount)
            if let rawURL {
                fx.defaults.set(rawURL, forKey: OSSConfig.baseURLDefaultsKey)
            }
            let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
            if let rawToken {
                _ = config.setBridgeToken(rawToken)
            }
            XCTAssertFalse(config.isConfigured)

            let puller = OSSEpisodePuller(client: client)
            let store = LibraryStore(
                defaults: fx.defaults,
                loadLibraryOnInit: true,
                documentsDirectory: fx.rootTemp,
                ossConfig: config,
                ossPuller: puller
            )
            store.remember(id: "ep-local", seconds: 42, duration: 120)
            let progressBefore = fx.defaults.data(forKey: "PrivateListen.progress")

            store.refreshLocalLibraryOnActivation()
            await store.waitForOSSPull()

            XCTAssertEqual(StubOSSURLProtocol.requests(for: fx.bridgeHost).count, 0)
            let errorFile = fx.rootTemp.appendingPathComponent(".library-activation-oss-pull-error.json")
            XCTAssertFalse(FileManager.default.fileExists(atPath: errorFile.path))
            let afterCatalog = try Data(contentsOf: fx.libraryRoot.appendingPathComponent("library.json"))
            XCTAssertEqual(afterCatalog, beforeCatalog)
            XCTAssertEqual(fx.defaults.data(forKey: "PrivateListen.progress"), progressBefore)
        }
    }

    // 2. Token 迁移与脱敏
    func testLegacyTokenMigrationAndKeychainFailure() throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        fx.defaults.set("legacy-token-123", forKey: OSSConfig.legacyTokenDefaultsKey)
        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)

        XCTAssertEqual(config.bridgeToken, "legacy-token-123")
        XCTAssertNil(fx.defaults.string(forKey: OSSConfig.legacyTokenDefaultsKey))
        XCTAssertEqual(
            fx.secretStore.read(service: OSSConfig.keychainService, account: OSSConfig.keychainAccount),
            "legacy-token-123"
        )

        // Keychain write failure path: fail-closed, does not crash, does not treat plaintext as configured
        let failingStore = InMemoryOSSSecretStore()
        failingStore.failWrites = true
        fx.defaults.set(fx.baseURLString, forKey: OSSConfig.baseURLDefaultsKey)
        fx.defaults.set("unmigrated-secret", forKey: OSSConfig.legacyTokenDefaultsKey)
        let failConfig = OSSConfig(defaults: fx.defaults, secretStore: failingStore)

        XCTAssertNil(failConfig.bridgeToken)
        XCTAssertFalse(failConfig.isConfigured)
        XCTAssertEqual(fx.defaults.string(forKey: OSSConfig.legacyTokenDefaultsKey), "unmigrated-secret")
    }

    // 3. 正常按集拉库与合并（不依赖真实集数）
    func testIncrementalPullPreservesLocalEpisodesNotesAndMarks() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        _ = try writeLocalEpisode(
            id: "ep-local-only",
            title: "Local Original Title",
            in: fx.libraryRoot,
            extraRowFields: ["phone_only": "keep-me"]
        )
        let notesURL = fx.rootTemp.appendingPathComponent("listen_notes.json")
        let alongURL = fx.rootTemp.appendingPathComponent("listen_along_notes.json")
        let notesData = Data("{\"ep-local-only\":\"my note\"}".utf8)
        let alongData = Data("[{\"id\":\"1\"}]".utf8)
        try notesData.write(to: notesURL, options: .atomic)
        try alongData.write(to: alongURL, options: .atomic)

        let remoteAudio = Data("remote-mp3-bytes-ep-remote-new".utf8)
        let remoteDoc = Data("{\"segments\":[]}".utf8)
        let token = "test-bridge-token"

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
            let path = req.url?.path ?? ""
            if path == "/v1/library" {
                let payload: [String: Any] = [
                    "ok": true,
                    "episodes": [
                        ["id": "ep-local-only", "title": "Remote Overwrite Attempt", "file": "output.mp3"],
                        ["id": "ep-remote-new", "title": "Remote New Episode", "duration_sec": 95.0, "file": "output.mp3"],
                    ],
                ]
                let data = try! JSONSerialization.data(withJSONObject: payload)
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: data)
            }
            if path == "/v1/signed-get",
               let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
               let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
               let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                let signed = "https://\(fx.ossHost)/\(workId)/\(name)?Signature=abc"
                let data = try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed])
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: data)
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
            let path = req.url?.path ?? ""
            if path == "/ep-remote-new/output.mp3" {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "audio/mpeg"], body: remoteAudio)
            }
            if path == "/ep-remote-new/document.json" {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "application/json"], body: remoteDoc)
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken(token))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let puller = OSSEpisodePuller(client: client, successCooldown: 60)
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: puller
        )
        store.remember(id: "ep-local-only", seconds: 33, duration: 120)
        let localBefore = try XCTUnwrap(store.episodes.first { $0.id == "ep-local-only" })

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        // Local episode untouched
        let localAfter = try XCTUnwrap(store.episodes.first { $0.id == "ep-local-only" })
        XCTAssertEqual(localAfter.title, "Local Original Title")
        XCTAssertEqual(localAfter.audioURL, localBefore.audioURL)
        XCTAssertEqual(store.resumeSeconds(for: "ep-local-only"), 33, accuracy: 0.01)

        // Remote new episode playable on disk
        let remoteAfter = try XCTUnwrap(store.episodes.first { $0.id == "ep-remote-new" })
        XCTAssertTrue(remoteAfter.isPlayableNow)
        XCTAssertTrue(LocalAudio.isPlayable(remoteAfter.audioURL))
        XCTAssertTrue(remoteAfter.audioURL.path.contains("/objects/"))

        // phone_only preserved in library.json
        let catalogData = try Data(contentsOf: fx.libraryRoot.appendingPathComponent("library.json"))
        let snap = try LibrarySnapshot(catalogData)
        let localRow = snap.rows.first { ($0["id"] as? String) == "ep-local-only" }
        XCTAssertEqual(localRow?["phone_only"] as? String, "keep-me")

        // Notes untouched byte-for-byte
        XCTAssertEqual(try Data(contentsOf: notesURL), notesData)
        XCTAssertEqual(try Data(contentsOf: alongURL), alongData)

        // Presigned OSS downloads never received X-Shengjian-Token
        let ossRequests = StubOSSURLProtocol.requests(for: fx.ossHost)
        XCTAssertFalse(ossRequests.isEmpty)
        XCTAssertTrue(ossRequests.allSatisfy { $0.tokenHeader == nil })

        // Cold restart still sees both episodes playable
        let coldStore = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: puller
        )
        let coldRemote = try XCTUnwrap(coldStore.episodes.first { $0.id == "ep-remote-new" })
        XCTAssertTrue(coldRemote.isPlayableNow)
        XCTAssertTrue(LocalAudio.isPlayable(coldRemote.audioURL))

        // Second activation hits cooldown -> zero extra requests
        let countBefore = StubOSSURLProtocol.requests(for: fx.bridgeHost).count
        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()
        XCTAssertEqual(StubOSSURLProtocol.requests(for: fx.bridgeHost).count, countBefore)
    }

    // 4. /v1/library 全类失败安静降级
    func testLibraryFailuresDegradeQuietlyAndCooldown() async throws {
        let failureModes: [(String, StubOSSURLProtocol.StubResponse)] = [
            ("empty-502", StubOSSURLProtocol.StubResponse(statusCode: 502, body: Data())),
            ("json-502", StubOSSURLProtocol.StubResponse(statusCode: 502, body: Data("{\"ok\":false,\"error\":\"library_invalid_json\"}".utf8))),
            ("401", StubOSSURLProtocol.StubResponse(statusCode: 401, body: Data())),
            ("bad-json", StubOSSURLProtocol.StubResponse(statusCode: 200, body: Data("not-json".utf8))),
            ("timeout", StubOSSURLProtocol.StubResponse(error: URLError(.timedOut))),
        ]

        for (label, stub) in failureModes {
            let fx = try makeFixture()
            defer { fx.cleanup() }

            let beforeCatalog = try writeLocalEpisode(id: "ep-local", title: "Local \(label)", in: fx.libraryRoot)
            let token = "secret-token-\(label)"

            StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { _ in stub }

            let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
            config.setBridgeBaseURL(fx.baseURLString)
            XCTAssertTrue(config.setBridgeToken(token))

            let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
            let puller = OSSEpisodePuller(client: client, failureCooldown: 300)
            let store = LibraryStore(
                defaults: fx.defaults,
                loadLibraryOnInit: true,
                documentsDirectory: fx.rootTemp,
                ossConfig: config,
                ossPuller: puller
            )
            store.remember(id: "ep-local", seconds: 15, duration: 120)
            let progressBefore = fx.defaults.data(forKey: "PrivateListen.progress")

            store.refreshLocalLibraryOnActivation()
            await store.waitForOSSPull()

            XCTAssertNil(store.message, "Failure \(label) must not touch library.message")
            XCTAssertEqual(store.episodes.count, 1)
            XCTAssertEqual(try Data(contentsOf: fx.libraryRoot.appendingPathComponent("library.json")), beforeCatalog)
            XCTAssertEqual(fx.defaults.data(forKey: "PrivateListen.progress"), progressBefore)

            let errorFile = fx.rootTemp.appendingPathComponent(".library-activation-oss-pull-error.json")
            let errorBytes = try Data(contentsOf: errorFile)
            let errorText = String(decoding: errorBytes, as: UTF8.self)
            XCTAssertFalse(errorText.contains(token), "Error log leaked token for \(label)")
            XCTAssertFalse(errorText.contains("Signature="), "Error log leaked presigned query for \(label)")

            let reqs = StubOSSURLProtocol.requests(for: fx.bridgeHost)
            XCTAssertEqual(reqs.count, 1, "Expected 1 request for \(label)")
            XCTAssertFalse(reqs.contains { $0.url.path.contains("signed-get") })

            // Immediate second activation is blocked by cooldown / 401 lockout
            store.refreshLocalLibraryOnActivation()
            await store.waitForOSSPull()
            XCTAssertEqual(StubOSSURLProtocol.requests(for: fx.bridgeHost).count, 1)
        }
    }

    // 5. 单集部分失败隔离 & 坏载荷拒收
    func testPartialEpisodeFailureIsolationAndBadPayloads() async throws {
        let badAudioCases: [(String, StubOSSURLProtocol.StubResponse)] = [
            ("html-200", StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "text/html; charset=utf-8"], body: Data("<html>login</html>".utf8))),
            ("empty-0-bytes", StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "audio/mpeg"], body: Data())),
            ("oss-502", StubOSSURLProtocol.StubResponse(statusCode: 502, body: Data())),
        ]

        for (label, badAudioStub) in badAudioCases {
            let fx = try makeFixture()
            defer { fx.cleanup() }

            let goodAudio = Data("good-audio-\(label)".utf8)
            StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
                if req.url?.path == "/v1/library" {
                    let payload: [String: Any] = [
                        "ok": true,
                        "episodes": [
                            ["id": "ep-bad", "title": "Bad Episode", "file": "output.mp3"],
                            ["id": "ep-good", "title": "Good Episode", "file": "output.mp3"],
                        ],
                    ]
                    return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: payload))
                }
                if req.url?.path == "/v1/signed-get",
                   let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
                   let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
                   let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                    let signed = "https://\(fx.ossHost)/\(workId)/\(name)?Signature=xyz"
                    return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed]))
                }
                return StubOSSURLProtocol.StubResponse(statusCode: 404)
            }

            StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
                let path = req.url?.path ?? ""
                if path == "/ep-bad/output.mp3" { return badAudioStub }
                if path == "/ep-good/output.mp3" {
                    return StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "audio/mpeg"], body: goodAudio)
                }
                return StubOSSURLProtocol.StubResponse(statusCode: 404)
            }

            let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
            config.setBridgeBaseURL(fx.baseURLString)
            XCTAssertTrue(config.setBridgeToken("tok"))

            let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
            let puller = OSSEpisodePuller(client: client)
            let store = LibraryStore(
                defaults: fx.defaults,
                loadLibraryOnInit: true,
                documentsDirectory: fx.rootTemp,
                ossConfig: config,
                ossPuller: puller
            )

            store.refreshLocalLibraryOnActivation()
            await store.waitForOSSPull()

            XCTAssertNil(store.episodes.first { $0.id == "ep-bad" })
            let good = try XCTUnwrap(store.episodes.first { $0.id == "ep-good" })
            XCTAssertTrue(good.isPlayableNow)
        }
    }

    // 6. 远端 pendingTranscript 行跳过 & 缺封面降级
    func testPendingTranscriptSkippedAndMissingArtworkDegraded() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        let audio = Data("audio-no-cover".utf8)
        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
            if req.url?.path == "/v1/library" {
                let payload: [String: Any] = [
                    "ok": true,
                    "episodes": [
                        [
                            "id": "ep-pending-skip",
                            "title": "Pending Review",
                            "audio_kind": "original_audio",
                            "status": "pending_review",
                            "transcript_status": "asr_unreviewed",
                            "file": "output.mp3",
                        ],
                        [
                            "id": "ep-missing-cover",
                            "title": "Missing Cover",
                            "artwork": "cover.jpg",
                            "artwork_kind": "provided",
                            "file": "output.mp3",
                        ],
                    ],
                ]
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: payload))
            }
            if req.url?.path == "/v1/signed-get",
               let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
               let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
               let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                let signed = "https://\(fx.ossHost)/\(workId)/\(name)?Signature=sig"
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed]))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
            if req.url?.path == "/ep-missing-cover/output.mp3" {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "audio/mpeg"], body: audio)
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken("tok"))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: OSSEpisodePuller(client: client)
        )

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        XCTAssertNil(store.episodes.first { $0.id == "ep-pending-skip" })
        let ep = try XCTUnwrap(store.episodes.first { $0.id == "ep-missing-cover" })
        XCTAssertTrue(ep.isPlayableNow)
        XCTAssertNil(ep.artworkFile)

        let snap = try LibrarySnapshot(Data(contentsOf: fx.libraryRoot.appendingPathComponent("library.json")))
        let row = try XCTUnwrap(snap.rows.first { ($0["id"] as? String) == "ep-missing-cover" })
        XCTAssertNil(row["artwork"])
        XCTAssertNil(row["artwork_kind"])
    }

    // 7. 空目录防误覆盖（D3 边界）
    func testNonEmptyDocumentsDirectoryWithoutCatalogFailsClosed() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        let existingDir = fx.libraryRoot.appendingPathComponent("episodes/existing", isDirectory: true)
        try FileManager.default.createDirectory(at: existingDir, withIntermediateDirectories: true)
        try Data("existing-audio".utf8).write(to: existingDir.appendingPathComponent("output.mp3"))

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
            if req.url?.path == "/v1/library" {
                let payload: [String: Any] = [
                    "ok": true,
                    "episodes": [["id": "ep-remote", "title": "Remote", "file": "output.mp3"]],
                ]
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: payload))
            }
            if req.url?.path == "/v1/signed-get",
               let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
               let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
               let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                let signed = "https://\(fx.ossHost)/\(workId)/\(name)"
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed]))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }
        StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
            if req.url?.path == "/ep-remote/output.mp3" {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: Data("remote-audio".utf8))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken("tok"))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: OSSEpisodePuller(client: client)
        )

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        XCTAssertFalse(FileManager.default.fileExists(atPath: fx.libraryRoot.appendingPathComponent("library.json").path))
    }

    // 8. 既有本地 library.json 损坏 fail-closed
    func testCorruptLocalCatalogFailsClosed() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        try FileManager.default.createDirectory(at: fx.libraryRoot, withIntermediateDirectories: true)
        let corruptBytes = Data("{corrupt-catalog".utf8)
        let listing = fx.libraryRoot.appendingPathComponent("library.json")
        try corruptBytes.write(to: listing)

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
            if req.url?.path == "/v1/library" {
                let payload: [String: Any] = [
                    "ok": true,
                    "episodes": [["id": "ep-remote", "title": "Remote", "file": "output.mp3"]],
                ]
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: payload))
            }
            if req.url?.path == "/v1/signed-get",
               let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
               let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
               let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                let signed = "https://\(fx.ossHost)/\(workId)/\(name)"
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed]))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }
        StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
            if req.url?.path == "/ep-remote/output.mp3" {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: Data("remote-audio".utf8))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken("tok"))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: OSSEpisodePuller(client: client)
        )

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        XCTAssertEqual(try Data(contentsOf: listing), corruptBytes)
    }

    // 9. iCloud 激活态不越界
    func testCloudFolderSelectedSkipsOSSPull() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        let cloudSim = URL(fileURLWithPath: "/tmp/oss-cloud-sim-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: cloudSim, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: cloudSim) }

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { _ in
            XCTFail("OSS pull must be skipped when folderURL != nil")
            return StubOSSURLProtocol.StubResponse(statusCode: 500)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken("tok"))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: false,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: OSSEpisodePuller(client: client)
        )
        store.adopt(cloudSim)
        XCTAssertTrue(store.hasCloudFolder)

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        XCTAssertEqual(StubOSSURLProtocol.requests(for: fx.bridgeHost).count, 0)
        let contents = try FileManager.default.contentsOfDirectory(atPath: cloudSim.path)
        XCTAssertTrue(contents.isEmpty)
    }

    // 10. 每轮上限与逐集持久化
    func testMaxEpisodesPerActivationLimit() async throws {
        let fx = try makeFixture()
        defer { fx.cleanup() }

        StubOSSURLProtocol.register(scopeHost: fx.bridgeHost) { req in
            if req.url?.path == "/v1/library" {
                let rows = (1...5).map { idx in
                    ["id": "ep-\(idx)", "title": "Episode \(idx)", "file": "output.mp3"] as [String: Any]
                }
                let payload: [String: Any] = ["ok": true, "episodes": rows]
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: payload))
            }
            if req.url?.path == "/v1/signed-get",
               let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false),
               let workId = comps.queryItems?.first(where: { $0.name == "work_id" })?.value,
               let name = comps.queryItems?.first(where: { $0.name == "name" })?.value {
                let signed = "https://\(fx.ossHost)/\(workId)/\(name)"
                return StubOSSURLProtocol.StubResponse(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: ["ok": true, "url": signed]))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }
        StubOSSURLProtocol.register(scopeHost: fx.ossHost) { req in
            if req.url?.path.hasSuffix("/output.mp3") == true {
                return StubOSSURLProtocol.StubResponse(statusCode: 200, headers: ["Content-Type": "audio/mpeg"], body: Data("audio-bytes".utf8))
            }
            return StubOSSURLProtocol.StubResponse(statusCode: 404)
        }

        let config = OSSConfig(defaults: fx.defaults, secretStore: fx.secretStore)
        config.setBridgeBaseURL(fx.baseURLString)
        XCTAssertTrue(config.setBridgeToken("tok"))

        let client = OSSLibraryClient(session: StubOSSURLProtocol.makeSession())
        let puller = OSSEpisodePuller(client: client, maxEpisodesPerActivation: 2)
        let store = LibraryStore(
            defaults: fx.defaults,
            loadLibraryOnInit: true,
            documentsDirectory: fx.rootTemp,
            ossConfig: config,
            ossPuller: puller
        )

        store.refreshLocalLibraryOnActivation()
        await store.waitForOSSPull()

        XCTAssertEqual(store.episodes.count, 2)
        XCTAssertTrue(store.episodes.allSatisfy { $0.isPlayableNow })
    }
}
