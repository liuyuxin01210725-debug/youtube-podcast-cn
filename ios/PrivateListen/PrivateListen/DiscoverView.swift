import SwiftUI

/// Theme-primary discover: lens chips first; people/shows nest inside a lens.
/// Quiet paper-ish surface — sectioned like Bayt zones, not Bayt chrome.
struct DiscoverView: View {
    @EnvironmentObject private var library: LibraryStore
    @State private var selected: DiscoverLens?
    @State private var articleURL = ""
    @State private var articleNote: String?

    private let paper = Color(red: 0.97, green: 0.96, blue: 0.93)
    private let ink = Color(red: 0.18, green: 0.17, blue: 0.15)
    private let mute = Color(red: 0.45, green: 0.42, blue: 0.38)

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("按主题听，不按网红货架。人与节目挂在透镜里。")
                        .font(.subheadline)
                        .foregroundStyle(mute)

                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(DiscoverCatalog.lenses) { lens in
                                let on = selected?.id == lens.id
                                Button {
                                    selected = lens
                                } label: {
                                    Text(lens.label_zh)
                                        .font(.subheadline.weight(on ? .semibold : .regular))
                                        .foregroundStyle(on ? paper : ink)
                                        .padding(.horizontal, 14)
                                        .padding(.vertical, 8)
                                        .background(on ? ink.opacity(0.92) : Color.white.opacity(0.75), in: Capsule())
                                        .overlay(Capsule().stroke(ink.opacity(0.12), lineWidth: 1))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(.vertical, 2)
                    }

                    if let lens = selected {
                        lensDetail(lens)
                    } else {
                        Text("选一个主题透镜")
                            .font(.footnote)
                            .foregroundStyle(mute)
                    }

                    articlePaste
                }
                .padding(20)
            }
            .background(paper.ignoresSafeArea())
            .navigationTitle("发现")
            .onAppear {
                if selected == nil {
                    selected = DiscoverCatalog.lenses.first
                }
            }
        }
    }

    private func lensDetail(_ lens: DiscoverLens) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(lens.label_zh)
                .font(.title3.weight(.semibold))
                .foregroundStyle(ink)

            let people = peopleChips(for: lens)
            if !people.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("人 / 节目")
                        .font(.caption)
                        .foregroundStyle(mute)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(people, id: \.self) { name in
                                Text(name)
                                    .font(.caption)
                                    .foregroundStyle(ink)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 6)
                                    .background(Color.white.opacity(0.85), in: Capsule())
                                    .overlay(Capsule().stroke(ink.opacity(0.08), lineWidth: 1))
                            }
                        }
                    }
                }
            }

            if lens.shows.isEmpty {
                Text(lens.placeholder ?? "暂无日扫源。可粘贴文章链接，或等后续信源。")
                    .font(.body)
                    .foregroundStyle(mute)
                    .padding(.vertical, 8)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(lens.shows) { show in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(show.name)
                                .font(.body.weight(.medium))
                                .foregroundStyle(ink)
                            Text(show.role ?? "占位 · 日扫后过三问卡才提案")
                                .font(.footnote)
                                .foregroundStyle(mute)
                        }
                        .padding(.vertical, 12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Divider().overlay(ink.opacity(0.08))
                    }
                }
                .padding(.horizontal, 14)
                .background(Color.white.opacity(0.72), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        }
    }

    private var articlePaste: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("粘贴文章链接")
                .font(.headline)
                .foregroundStyle(ink)
            Text("写入收件箱，Mac 抽正文后入队（DeepSeek + Edge；默认只记账，不立刻整篇 TTS）。")
                .font(.caption)
                .foregroundStyle(mute)
            TextField("https://…", text: $articleURL)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                .padding(12)
                .background(Color.white.opacity(0.9), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            Button("放入收件箱") { submitArticle() }
                .buttonStyle(.borderedProminent)
                .tint(ink)
            if let articleNote {
                Text(articleNote)
                    .font(.footnote)
                    .foregroundStyle(mute)
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.55), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func peopleChips(for lens: DiscoverLens) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for show in lens.shows {
            let people = show.people ?? []
            if people.isEmpty {
                if seen.insert(show.name).inserted { out.append(show.name) }
            } else {
                for person in people where seen.insert(person).inserted {
                    out.append(person)
                }
            }
        }
        return out
    }

    private func submitArticle() {
        let raw = articleURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: raw), InboxDrop.accepts(url) else {
            articleNote = "请粘贴公开的 HTTPS 文章链接。"
            return
        }
        if let host = url.host?.lowercased(), host.contains("youtube.com") || host == "youtu.be" {
            articleNote = "YouTube 请用分享到「私人听」，不要走文章入口。"
            return
        }
        library.acceptShare(url: url, sourceApp: "article-paste", contentType: "article", expectedSpeakers: 1)
        articleNote = library.message ?? "已写入收件箱。"
        articleURL = ""
    }
}
