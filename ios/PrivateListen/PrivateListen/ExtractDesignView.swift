import SwiftUI

/// Founder-facing spec reader. Pushed from the library menu; not a tab and not a marketing page.
struct ExtractDesignView: View {
    private let blocks: [ExtractDesignDocument.Block]

    init() {
        blocks = ExtractDesignDocument.blocks()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                decisions
                ForEach(blocks) { block in
                    blockView(block)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
            .padding(.bottom, 36)
        }
        .background(Color(uiColor: .systemBackground))
        .navigationTitle("收藏≠拥有")
        .navigationBarTitleDisplayMode(.inline)
        .textSelection(.enabled)
    }

    private var decisions: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("已定")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            decision("①", "收藏 = 深抽")
            decision("②", "声间内库 + Markdown 导出")
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("已定。第一，收藏等于深抽。第二，声间内库，并导出 Markdown。")
    }

    private func decision(_ index: String, _ title: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(index)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 22, alignment: .leading)
            Text(title)
                .font(.body.weight(.semibold))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func blockView(_ block: ExtractDesignDocument.Block) -> some View {
        switch block.kind {
        case .heading(let level, let text):
            InlineMarkdown(text, font: headingFont(level), weight: .semibold)
                .padding(.top, level <= 2 ? 10 : 2)
        case .paragraph(let text):
            InlineMarkdown(text)
        case .quote(let text):
            InlineMarkdown(text, font: .callout, color: .secondary)
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle()
                        .fill(Color.primary.opacity(0.22))
                        .frame(width: 2)
                }
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("·")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 12, alignment: .center)
                        InlineMarkdown(item)
                    }
                }
            }
        case .numbered(let items):
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(verbatim: item.marker)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .frame(minWidth: 18, alignment: .trailing)
                        InlineMarkdown(item.text)
                    }
                }
            }
        case .table(let headers, let rows):
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    tableRow(headers: headers, row: row)
                    if index < rows.count - 1 {
                        Divider()
                    }
                }
            }
        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(verbatim: text)
                    .font(.system(.footnote, design: .monospaced))
                    .foregroundStyle(.primary)
                    .padding(12)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        case .rule:
            Divider()
                .padding(.vertical, 2)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title2
        case 2: return .title3
        default: return .headline
        }
    }

    @ViewBuilder
    private func tableRow(headers: [String], row: [String]) -> some View {
        let columnCount = max(headers.count, row.count)
        VStack(alignment: .leading, spacing: 6) {
            if columnCount <= 2 {
                twoColumnRow(row)
            } else {
                ForEach(0..<columnCount, id: \.self) { index in
                    stackedCell(
                        header: index < headers.count ? headers[index] : "",
                        value: index < row.count ? row[index] : ""
                    )
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private func twoColumnRow(_ row: [String]) -> some View {
        let title = row.first ?? ""
        let detail = row.count > 1 ? row[1] : ""
        if !title.isEmpty {
            InlineMarkdown(title, font: .subheadline, weight: .semibold)
        }
        if !detail.isEmpty {
            InlineMarkdown(detail, font: .subheadline, color: .secondary)
        }
    }

    @ViewBuilder
    private func stackedCell(header: String, value: String) -> some View {
        if !header.isEmpty || !value.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                if !header.isEmpty {
                    InlineMarkdown(header, font: .caption2, color: .secondary)
                }
                if !value.isEmpty {
                    InlineMarkdown(value, font: .subheadline)
                }
            }
        }
    }
}

/// Renders a single Markdown line: bold, inline code, and link labels. Links stay in the page.
private struct InlineMarkdown: View {
    let source: String
    var font: Font = .body
    var color: Color = .primary
    var weight: Font.Weight = .regular

    init(_ source: String, font: Font = .body, color: Color = .primary, weight: Font.Weight = .regular) {
        self.source = source
        self.font = font
        self.color = color
        self.weight = weight
    }

    var body: some View {
        rendered
            .foregroundStyle(color)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var rendered: Text {
        var text = Text(verbatim: "")
        for run in Self.runs(in: source) {
            switch run {
            case .plain(let value):
                text = text + Text(verbatim: value).font(font).fontWeight(weight)
            case .bold(let value):
                text = text + Text(verbatim: value).font(font).fontWeight(.semibold)
            case .code(let value):
                text = text + Text(verbatim: value).font(.system(.callout, design: .monospaced))
            }
        }
        return text
    }

    private enum Run {
        case plain(String)
        case bold(String)
        case code(String)
    }

    private static func runs(in source: String) -> [Run] {
        let source = stripLinks(source)
        var runs: [Run] = []
        var plain = ""
        var index = source.startIndex

        func flush() {
            if !plain.isEmpty {
                runs.append(.plain(plain))
                plain = ""
            }
        }

        while index < source.endIndex {
            let character = source[index]
            if character == "`" {
                let next = source.index(after: index)
                if next <= source.endIndex, let end = source[next...].firstIndex(of: "`") {
                    flush()
                    runs.append(.code(String(source[next..<end])))
                    index = source.index(after: end)
                    continue
                }
            }
            if character == "*" {
                let next = source.index(after: index)
                if next < source.endIndex, source[next] == "*" {
                    let start = source.index(after: next)
                    if let range = source[start...].range(of: "**") {
                        flush()
                        runs.append(.bold(String(source[start..<range.lowerBound])))
                        index = range.upperBound
                        continue
                    }
                }
            }
            plain.append(character)
            index = source.index(after: index)
        }
        flush()
        if runs.isEmpty { runs.append(.plain("")) }
        return runs
    }

    private static func stripLinks(_ source: String) -> String {
        guard let expression = try? NSRegularExpression(pattern: #"\[([^\]]+)\]\(([^)]+)\)"#) else {
            return source
        }
        let range = NSRange(source.startIndex..<source.endIndex, in: source)
        return expression.stringByReplacingMatches(in: source, range: range, withTemplate: "$1")
    }
}
