import Foundation

enum OSSClientError: Error, Equatable, Sendable {
    case notConfigured
    case unauthorized(Int)
    case bridgeHTTPStatus(Int, code: String?)
    case invalidCatalogPayload
    case invalidPresignPayload
    case downloadHTTPStatus(Int)
    case emptyDownload
    case unexpectedHTMLPayload
    case transportFailure(code: Int)

    var diagnosticCode: String {
        switch self {
        case .notConfigured:
            return "notConfigured"
        case .unauthorized(let status):
            return "unauthorized(\(status))"
        case .bridgeHTTPStatus(let status, let code):
            if let code {
                return "bridgeHTTPStatus(\(status), code: \(code))"
            }
            return "bridgeHTTPStatus(\(status), code: nil)"
        case .invalidCatalogPayload:
            return "invalidCatalogPayload"
        case .invalidPresignPayload:
            return "invalidPresignPayload"
        case .downloadHTTPStatus(let status):
            return "downloadHTTPStatus(\(status))"
        case .emptyDownload:
            return "emptyDownload"
        case .unexpectedHTMLPayload:
            return "unexpectedHTMLPayload"
        case .transportFailure(let code):
            return "transportFailure(code: \(code))"
        }
    }
}

struct OSSRemoteCatalogItem: Sendable {
    let id: String
    let sanitizedRowData: Data
    let declaredArtwork: (name: String, kind: String?)?
    let declaredRevisionFiles: [String: String]?
    let declaredRevisionSHA256: String?
    let declaresPendingTranscript: Bool
}

final class OSSLibraryClient: @unchecked Sendable {
    static let allowedArtifactNames: Set<String> = [
        "output.mp3",
        "document.json",
        "source-identity.json",
        "render-identity.json",
        "episode.json",
        "extract.json",
        "point-listen.json",
        "cover.jpg",
        "cover.png",
        "cover.webp",
    ]

    private static let allowedArtworkNames: Set<String> = [
        "cover.jpg",
        "cover.png",
        "cover.webp",
    ]

    private static let allowedArtworkKinds: Set<String> = [
        "provided",
        "thumbnail",
        "title",
        "placeholder",
    ]

    private static let strippedRowKeys: Set<String> = [
        "resume_seconds",
        "resume_sec",
        "position_seconds",
        "playback_progress",
        "row",
        "revision",
        "files",
        "previous_path",
        "media_revision",
    ]

    private let session: URLSession

    init(session: URLSession? = nil) {
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.urlCache = nil
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.httpCookieStorage = nil
            configuration.httpShouldSetCookies = false
            configuration.timeoutIntervalForRequest = 10
            configuration.timeoutIntervalForResource = 120
            self.session = URLSession(configuration: configuration)
        }
    }

    func fetchCatalog(baseURL: URL, token: String) async throws -> [OSSRemoteCatalogItem] {
        let endpoint = baseURL
            .appendingPathComponent("v1")
            .appendingPathComponent("library")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "GET"
        request.timeoutInterval = 10
        request.setValue(token, forHTTPHeaderField: "X-Shengjian-Token")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await performData(request)
        guard let http = response as? HTTPURLResponse else {
            throw OSSClientError.invalidCatalogPayload
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            throw OSSClientError.unauthorized(http.statusCode)
        }
        guard http.statusCode == 200 else {
            throw OSSClientError.bridgeHTTPStatus(http.statusCode, code: Self.sanitizedErrorCode(from: data))
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (object["ok"] as? Bool) == true,
              let rawEpisodes = object["episodes"] as? [[String: Any]] else {
            throw OSSClientError.invalidCatalogPayload
        }

        var items: [OSSRemoteCatalogItem] = []
        var seenIDs = Set<String>()
        let decoder = JSONDecoder.privateListen()

        for rawRow in rawEpisodes {
            let candidateID = (rawRow["id"] as? String)
                ?? (rawRow["episode_id"] as? String)
                ?? (rawRow["source_job_id"] as? String)
            guard let id = candidateID,
                  EpisodeID.isSafe(id),
                  seenIDs.insert(id).inserted else {
                continue
            }
            if let file = rawRow["file"] as? String, file != "output.mp3" {
                continue
            }

            let audioKind = rawRow["audio_kind"] as? String
            let transcriptStatus = rawRow["transcript_status"] as? String
            let status = rawRow["status"] as? String
            let transcript = rawRow["transcript"] as? String
            let declaresPending = audioKind == "original_audio"
                || transcriptStatus == "asr_unreviewed"
                || status == "pending_review"
                || transcript == "transcript.pending.json"

            var artworkDeclaration: (name: String, kind: String?)?
            if let artName = rawRow["artwork"] as? String,
               Self.allowedArtworkNames.contains(artName) {
                let artKind = rawRow["artwork_kind"] as? String
                if artKind == nil || Self.allowedArtworkKinds.contains(artKind ?? "") {
                    artworkDeclaration = (artName, artKind)
                }
            }

            var revisionFiles: [String: String]?
            var revisionSHA256: String?
            if let revRaw = rawRow["media_revision"] as? [String: Any],
               let revData = try? LibrarySnapshot.json(revRaw),
               let decoded = try? decoder.decode(MediaRevision.self, from: revData),
               decoded.isValid(for: id) {
                revisionFiles = decoded.files
                revisionSHA256 = decoded.sha256
            }

            var sanitizedRow = rawRow.filter { !Self.strippedRowKeys.contains($0.key) }
            sanitizedRow["id"] = id
            sanitizedRow["file"] = "output.mp3"
            if artworkDeclaration == nil {
                sanitizedRow.removeValue(forKey: "artwork")
                sanitizedRow.removeValue(forKey: "artwork_kind")
            }
            guard let sanitizedRowData = try? LibrarySnapshot.json(sanitizedRow) else {
                continue
            }

            items.append(OSSRemoteCatalogItem(
                id: id,
                sanitizedRowData: sanitizedRowData,
                declaredArtwork: artworkDeclaration,
                declaredRevisionFiles: revisionFiles,
                declaredRevisionSHA256: revisionSHA256,
                declaresPendingTranscript: declaresPending
            ))
        }
        return items
    }

    func fetchSignedURL(
        baseURL: URL,
        token: String,
        workId: String,
        name: String,
        expires: Int = 900
    ) async throws -> URL {
        guard EpisodeID.isSafe(workId),
              Self.allowedArtifactNames.contains(name),
              expires > 0 else {
            throw OSSClientError.invalidPresignPayload
        }
        let endpoint = baseURL
            .appendingPathComponent("v1")
            .appendingPathComponent("signed-get")
        guard var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) else {
            throw OSSClientError.invalidPresignPayload
        }
        components.queryItems = [
            URLQueryItem(name: "work_id", value: workId),
            URLQueryItem(name: "name", value: name),
            URLQueryItem(name: "expires", value: String(expires)),
        ]
        guard let url = components.url else {
            throw OSSClientError.invalidPresignPayload
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 10
        request.setValue(token, forHTTPHeaderField: "X-Shengjian-Token")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await performData(request)
        guard let http = response as? HTTPURLResponse else {
            throw OSSClientError.invalidPresignPayload
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            throw OSSClientError.unauthorized(http.statusCode)
        }
        guard http.statusCode == 200 else {
            throw OSSClientError.bridgeHTTPStatus(http.statusCode, code: Self.sanitizedErrorCode(from: data))
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (object["ok"] as? Bool) == true,
              let rawSignedURL = object["url"] as? String,
              let signedURL = URL(string: rawSignedURL),
              let signedComponents = URLComponents(url: signedURL, resolvingAgainstBaseURL: false),
              let scheme = signedComponents.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              let host = signedComponents.host,
              !host.isEmpty,
              signedComponents.user == nil,
              signedComponents.password == nil else {
            throw OSSClientError.invalidPresignPayload
        }
        return signedURL
    }

    @discardableResult
    func downloadFile(from signedURL: URL, to destinationURL: URL, expectAudio: Bool) async throws -> Int {
        var request = URLRequest(url: signedURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 120
        // Never forward X-Shengjian-Token to OSS presigned URLs.

        let (tempURL, response) = try await performDownload(request)
        let fm = FileManager.default
        defer {
            if fm.fileExists(atPath: tempURL.path) {
                try? fm.removeItem(at: tempURL)
            }
        }

        guard let http = response as? HTTPURLResponse else {
            throw OSSClientError.downloadHTTPStatus(0)
        }
        guard http.statusCode == 200 else {
            throw OSSClientError.downloadHTTPStatus(http.statusCode)
        }
        if expectAudio,
           let mime = http.mimeType?.lowercased(),
           mime.hasPrefix("text/html") {
            throw OSSClientError.unexpectedHTMLPayload
        }
        let attributes = try fm.attributesOfItem(atPath: tempURL.path)
        let byteCount = (attributes[.size] as? NSNumber)?.intValue ?? 0
        guard byteCount > 0 else {
            throw OSSClientError.emptyDownload
        }

        try fm.createDirectory(at: destinationURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if fm.fileExists(atPath: destinationURL.path) {
            try fm.removeItem(at: destinationURL)
        }
        try fm.moveItem(at: tempURL, to: destinationURL)
        return byteCount
    }

    private func performData(_ request: URLRequest) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(for: request)
        } catch let error as URLError {
            throw OSSClientError.transportFailure(code: error.errorCode)
        } catch {
            throw OSSClientError.transportFailure(code: (error as NSError).code)
        }
    }

    private func performDownload(_ request: URLRequest) async throws -> (URL, URLResponse) {
        do {
            return try await session.download(for: request)
        } catch let error as URLError {
            throw OSSClientError.transportFailure(code: error.errorCode)
        } catch {
            throw OSSClientError.transportFailure(code: (error as NSError).code)
        }
    }

    private static func sanitizedErrorCode(from data: Data) -> String? {
        guard !data.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = object["error"] as? String else {
            return nil
        }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (1...48).contains(trimmed.count),
              trimmed.utf8.allSatisfy({
                  (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 95 || $0 == 45
              }) else {
            return nil
        }
        return trimmed
    }
}
