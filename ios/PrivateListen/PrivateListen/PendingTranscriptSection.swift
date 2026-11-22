import SwiftUI

struct PendingTranscriptSection: View {
    let episodeID: String
    let audioURL: URL
    let reference: PendingTranscriptReference

    @State private var result: Result<PendingTranscript, PendingTranscript.ReadFailure>?
    @State private var attempt = 0
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("待核稿")
                .font(.headline)
            switch result {
            case nil:
                ProgressView("正在校验稿件与音频")
            case .failure(let reason):
                Text(reason == .unavailable ? "缺少稿件或配套文件，暂时无法读取。" : "稿件与本集音频的绑定校验失败。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Button("重新校验") { attempt += 1 }
                    .font(.subheadline)
            case .success(let transcript):
                Text("\(transcript.segments.count) 段 · \(range(transcript.start, transcript.end))")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
                DisclosureGroup("阅读分段稿", isExpanded: $expanded) {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(transcript.segments) { segment in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(range(segment.start, segment.end))
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                                Text(segment.text)
                            }
                        }
                    }
                    .padding(.top, 8)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task(id: attempt) {
            result = nil
            let reader = Task.detached(priority: .utility) { [episodeID, audioURL, reference] in
                PendingTranscript.read(id: episodeID, audioURL: audioURL, reference: reference)
            }
            let checked = await withTaskCancellationHandler {
                await reader.value
            } onCancel: {
                reader.cancel()
            }
            guard !Task.isCancelled else { return }
            result = checked
        }
    }

    private func range(_ start: Double, _ end: Double) -> String {
        "\(start.formatted(.number))–\(end.formatted(.number)) 秒"
    }
}
