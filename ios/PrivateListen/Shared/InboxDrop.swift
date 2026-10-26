import Foundation

struct InboxEntry: Identifiable, Hashable {
    var id: String
    var url: String
    var status: String
    var detail: String
    var sharedAt: Date

    var label: String { InboxDrop.label(for: url) }

    var statusLabel: String {
        switch status {
        case "queued": return "正在转换"
        case "failed": return "转换失败"
        case "duplicate": return "已经收过"
        case "published": return "已发布"
        default: return "等待 Mac"
        }
    }

    var line: String {
        if detail.isEmpty { return statusLabel }
        return "\(statusLabel) · \(detail)"
    }
}

enum InboxDrop {
    static func accepts(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https", url.host != nil, url.user == nil else { return false }
        let raw = url.absoluteString
        guard !raw.isEmpty, raw.count <= 2000, !raw.contains(where: \.isWhitespace) else { return false }
        let host = url.host!.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        if host.isEmpty || host == "localhost" || host == "localhost.localdomain" || host.hasSuffix(".local") {
            return false
        }
        if isPrivateHost(host) { return false }
        return true
    }

    static func httpsURL(in text: String) -> URL? {
        let pattern = #"https://[^\s<>"']+"#
        guard let range = text.range(of: pattern, options: .regularExpression) else { return nil }
        var raw = String(text[range])
        while let last = raw.last, ".,);]}>".contains(last) {
            raw.removeLast()
        }
        guard let url = URL(string: raw), accepts(url) else { return nil }
        return url
    }

    static func write(url: URL, sourceApp: String?, into folder: URL) throws {
        guard accepts(url) else { throw InboxError.rejected }
        let inbox = folder.appendingPathComponent("inbox", isDirectory: true)
        try FileManager.default.createDirectory(at: inbox, withIntermediateDirectories: true)
        let ident = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        var object: [String: String] = [
            "id": ident,
            "url": url.absoluteString,
            "shared_at": timestamp(),
        ]
        if let sourceApp {
            let trimmed = sourceApp.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty, trimmed.count <= 120 {
                object["source_app"] = trimmed
            }
        }
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        let destination = inbox.appendingPathComponent("\(ident).json")
        let coordinator = NSFileCoordinator()
        var coordinationError: NSError?
        var writeError: Error?
        coordinator.coordinate(writingItemAt: destination, options: .forReplacing, error: &coordinationError) { target in
            do {
                try data.write(to: target, options: .atomic)
            } catch {
                writeError = error
            }
        }
        if coordinationError != nil || writeError != nil {
            try data.write(to: destination, options: .atomic)
        }
    }

    static func items(in folder: URL) -> [InboxEntry] {
        let inbox = folder.appendingPathComponent("inbox", isDirectory: true)
        let keys: Set<URLResourceKey> = [.isRegularFileKey]
        guard let children = try? FileManager.default.contentsOfDirectory(
            at: inbox,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsHiddenFiles]
        ) else { return [] }
        var entries: [InboxEntry] = []
        for child in children {
            let name = child.lastPathComponent
            guard name.hasSuffix(".json"), !name.hasSuffix(".status.json") else { continue }
            let ident = String(name.dropLast(5))
            guard isShareID(ident) else { continue }
            guard let record = jsonObject(at: child), let url = record["url"] as? String, !url.isEmpty else { continue }
            let statusFile = inbox.appendingPathComponent("\(ident).status.json")
            let statusRecord = jsonObject(at: statusFile) ?? [:]
            let status = (statusRecord["status"] as? String) ?? "pending"
            if status == "published" { continue }
            let detail = ((statusRecord["error"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            entries.append(InboxEntry(
                id: ident,
                url: url,
                status: status,
                detail: String(detail.prefix(80)),
                sharedAt: parseDate(record["shared_at"] as? String) ?? .distantPast
            ))
        }
        return entries.sorted { lhs, rhs in
            if lhs.sharedAt != rhs.sharedAt { return lhs.sharedAt > rhs.sharedAt }
            return lhs.id > rhs.id
        }
        .prefix(12)
        .map { $0 }
    }

    static func label(for raw: String) -> String {
        guard let url = URL(string: raw) else { return "链接" }
        let host = url.host ?? "链接"
        let tail = url.lastPathComponent.removingPercentEncoding ?? url.lastPathComponent
        if tail.isEmpty || tail == "/" || tail == host { return host }
        let short = tail.count > 42 ? String(tail.prefix(42)) + "…" : tail
        return "\(short) · \(host)"
    }

    private static func isPrivateHost(_ host: String) -> Bool {
        if host == "127.0.0.1" || host == "::1" || host == "0.0.0.0" { return true }
        if host.hasPrefix("10.") || host.hasPrefix("192.168.") || host.hasPrefix("169.254.") { return true }
        let parts = host.split(separator: ".")
        if parts.count == 4, parts[0] == "172", let second = Int(parts[1]), (16...31).contains(second) {
            return parts.allSatisfy { Int($0) != nil }
        }
        return false
    }

    private static func isShareID(_ ident: String) -> Bool {
        ident.count == 32 && ident.allSatisfy { "0123456789abcdef".contains($0) }
    }

    private static func timestamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: Date())
    }

    private static func parseDate(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    private static func jsonObject(at url: URL) -> [String: Any]? {
        let coordinator = NSFileCoordinator()
        var data: Data?
        var error: NSError?
        coordinator.coordinate(readingItemAt: url, options: [], error: &error) { readURL in
            data = try? Data(contentsOf: readURL)
        }
        if data == nil {
            data = try? Data(contentsOf: url)
        }
        guard let data, let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return object
    }
}

enum InboxError: Error {
    case rejected
}
