import Foundation

/// In-app reading copy of the 「收藏≠拥有」 spec. The Markdown ships in the app bundle.
enum ExtractDesignDocument {
    struct Block: Identifiable {
        let id: Int
        let kind: Kind
    }

    enum Kind {
        case heading(level: Int, text: String)
        case paragraph(String)
        case quote(String)
        case bullets([String])
        case numbered([Item])
        case table(headers: [String], rows: [[String]])
        case code(String)
        case rule
    }

    struct Item {
        var marker: String
        var text: String
    }

    static func markdown() -> String {
        if let url = Bundle.main.url(forResource: "ExtractDesign", withExtension: "md"),
           let text = try? String(contentsOf: url, encoding: .utf8) {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return text }
        }
        return """
        # 收藏≠拥有

        内置文稿没有放进 App。已定仍是：收藏等于深抽；抽出物以声间内状态机为第一库，并导出 Markdown。
        """
    }

    static func blocks() -> [Block] {
        parse(markdown())
    }

    static func parse(_ markdown: String) -> [Block] {
        let normalized = markdown
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        if normalized.hasPrefix("\u{FEFF}") {
            return parse(String(normalized.dropFirst()))
        }
        let lines = normalized.components(separatedBy: "\n")
        var blocks: [Block] = []
        var index = 0
        var nextID = 0

        func append(_ kind: Kind) {
            blocks.append(Block(id: nextID, kind: kind))
            nextID += 1
        }

        while index < lines.count {
            let raw = lines[index]
            let trimmed = raw.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty {
                index += 1
                continue
            }

            if trimmed.hasPrefix("```") {
                index += 1
                var code: [String] = []
                while index < lines.count && !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    code.append(lines[index])
                    index += 1
                }
                if index < lines.count { index += 1 }
                append(.code(code.joined(separator: "\n")))
                continue
            }

            if trimmed == "---" || trimmed == "***" {
                append(.rule)
                index += 1
                continue
            }

            if trimmed.hasPrefix("|") {
                var tableLines: [String] = []
                while index < lines.count && lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    tableLines.append(lines[index])
                    index += 1
                }
                if let table = tableKind(tableLines) {
                    append(table)
                }
                continue
            }

            if trimmed.hasPrefix(">") {
                var parts: [String] = []
                while index < lines.count {
                    let line = lines[index].trimmingCharacters(in: .whitespaces)
                    guard line.hasPrefix(">") else { break }
                    let body = line.dropFirst().trimmingCharacters(in: .whitespaces)
                    if !body.isEmpty { parts.append(body) }
                    index += 1
                }
                if !parts.isEmpty {
                    append(.quote(parts.joined(separator: " ")))
                }
                continue
            }

            if bulletBody(trimmed) != nil {
                var items: [String] = []
                while index < lines.count {
                    let line = lines[index]
                    let body = line.trimmingCharacters(in: .whitespaces)
                    if let item = bulletBody(body) {
                        items.append(item)
                        index += 1
                    } else if body.isEmpty {
                        break
                    } else if line.hasPrefix(" ") || line.hasPrefix("\t"), !items.isEmpty {
                        items[items.count - 1] += " " + body
                        index += 1
                    } else {
                        break
                    }
                }
                append(.bullets(items))
                continue
            }

            if numberedItem(trimmed) != nil {
                var items: [Item] = []
                while index < lines.count {
                    let body = lines[index].trimmingCharacters(in: .whitespaces)
                    if let item = numberedItem(body) {
                        items.append(item)
                        index += 1
                    } else {
                        break
                    }
                }
                append(.numbered(items))
                continue
            }

            if let heading = headingParts(trimmed) {
                append(.heading(level: heading.level, text: heading.text))
                index += 1
                continue
            }

            var paragraph: [String] = []
            while index < lines.count {
                let line = lines[index]
                let body = line.trimmingCharacters(in: .whitespaces)
                if body.isEmpty || startsBlock(body) { break }
                paragraph.append(body)
                index += 1
            }
            if !paragraph.isEmpty {
                append(.paragraph(paragraph.joined(separator: " ")))
            }
        }

        return blocks
    }

    private static func startsBlock(_ trimmed: String) -> Bool {
        if trimmed.hasPrefix("```") || trimmed.hasPrefix("|") || trimmed.hasPrefix(">") { return true }
        if trimmed == "---" || trimmed == "***" { return true }
        if headingParts(trimmed) != nil || bulletBody(trimmed) != nil || numberedItem(trimmed) != nil { return true }
        return false
    }

    private static func headingParts(_ trimmed: String) -> (level: Int, text: String)? {
        let level = trimmed.prefix { $0 == "#" }.count
        guard level >= 1, level <= 4 else { return nil }
        let rest = trimmed.dropFirst(level)
        guard rest.hasPrefix(" ") else { return nil }
        let text = rest.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        return (level, text)
    }

    private static func bulletBody(_ trimmed: String) -> String? {
        if trimmed.hasPrefix("- ") { return String(trimmed.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
        if trimmed.hasPrefix("* ") { return String(trimmed.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
        return nil
    }

    private static func numberedItem(_ trimmed: String) -> Item? {
        var digits = ""
        var index = trimmed.startIndex
        while index < trimmed.endIndex, trimmed[index].isNumber {
            digits.append(trimmed[index])
            index = trimmed.index(after: index)
        }
        guard !digits.isEmpty, index < trimmed.endIndex, trimmed[index] == "." else { return nil }
        let afterDot = trimmed.index(after: index)
        guard afterDot < trimmed.endIndex, trimmed[afterDot] == " " else { return nil }
        let text = trimmed[trimmed.index(after: afterDot)...].trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        return Item(marker: digits, text: text)
    }

    private static func tableKind(_ lines: [String]) -> Kind? {
        let parsed = lines.map(tableCells)
        guard let header = parsed.first, header.contains(where: { !$0.isEmpty }) else { return nil }
        var rows = Array(parsed.dropFirst())
        if let first = rows.first, isSeparator(first) {
            rows.removeFirst()
        }
        let data = rows.filter { $0.contains(where: { !$0.isEmpty }) && !isSeparator($0) }
        guard !data.isEmpty else { return nil }
        return .table(headers: header, rows: data)
    }

    private static func tableCells(_ line: String) -> [String] {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("|") { text.removeFirst() }
        if text.hasSuffix("|") { text.removeLast() }
        return text.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func isSeparator(_ cells: [String]) -> Bool {
        guard !cells.isEmpty else { return false }
        return cells.allSatisfy { cell in
            let core = cell.filter { $0 != "-" && $0 != ":" && !$0.isWhitespace }
            return core.isEmpty && cell.contains("-")
        }
    }
}
