import SwiftUI

struct ListenAlongSection: View {
    let episode: Episode
    @ObservedObject var player: PlayerModel
    let reading: PlayerReading?
    let onJumpToSeconds: (Double) -> Void
    let onScrollToListenNote: () -> Void

    @EnvironmentObject private var listenAlongNotes: ListenAlongNoteStore
    @EnvironmentObject private var listenNotes: ListenNoteStore

    @State private var isCapturing = false
    @State private var capturedPosition: Double?
    @State private var draftText = ""
    @State private var saveFailed = false
    @FocusState private var captureFocused: Bool

    private var episodeNotes: [ListenAlongNote] {
        listenAlongNotes.notes(for: episode.mediaKey, legacyEpisodeId: episode.id)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center) {
                Text("听中笔记")
                    .font(.headline)
                Spacer(minLength: 0)
                if listenAlongNotes.isReadOnly {
                    Text("只读")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Button("在此记下") {
                        capturedPosition = player.current.isFinite ? max(0, player.current) : 0
                        draftText = ""
                        saveFailed = false
                        isCapturing = true
                        captureFocused = true
                    }
                    .font(.subheadline.weight(.medium))
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .accessibilityIdentifier("player.listenAlong.capture")
                }
            }

            if listenAlongNotes.isReadOnly {
                Text("听中笔记读取或写入受限，原文件已保留。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Button("重新读取") { listenAlongNotes.reload() }
                    .font(.footnote)
            } else if isCapturing {
                captureBox
            }

            if episodeNotes.isEmpty {
                if !isCapturing && !listenAlongNotes.isReadOnly {
                    Text("点「在此记下」标记此刻位置。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(episodeNotes) { note in
                        noteRow(note)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: episode.mediaKey) { _, _ in
            cancelCapture()
        }
    }

    private var captureBox: some View {
        let position = capturedPosition ?? (player.current.isFinite ? max(0, player.current) : 0)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Text("记在 \(listenClock(position))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                if saveFailed {
                    Text("保存失败")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Button("取消") {
                    cancelCapture()
                }
                .font(.subheadline)
                Button("保存") {
                    saveCapture()
                }
                .font(.subheadline.weight(.semibold))
                .disabled(draftText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("player.listenAlong.save")
            }
            // 复用标准 TextField，仅通过 iOS 系统键盘听写输入；不申请麦克风权限，不自建录音或云端 STT。
            TextField("记下此刻想法", text: $draftText)
                .textFieldStyle(.roundedBorder)
                .submitLabel(.done)
                .focused($captureFocused)
                .onSubmit { saveCapture() }
                .accessibilityHint("先点输入框，再用系统键盘的麦克风听写；语音处理遵循你的系统与键盘设置。")
                .accessibilityIdentifier("player.listenAlong.editor")
            Text("先点输入框，再用系统键盘的麦克风听写；语音处理遵循你的系统与键盘设置。")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("player.listenAlong.dictationHint")
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    @ViewBuilder
    private func noteRow(_ note: ListenAlongNote) -> some View {
        let links = ListenAlongAssociation.links(
            for: note,
            points: reading?.nodes ?? [],
            listenNote: listenNotes.note(for: episode.id)
        )
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Button {
                    onJumpToSeconds(note.positionSec)
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(listenClock(note.positionSec))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                        Text(note.text)
                            .font(.subheadline)
                            .foregroundStyle(.primary)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("player.listenAlong.note.\(note.id)")

                if !listenAlongNotes.isReadOnly {
                    Button {
                        _ = listenAlongNotes.delete(id: note.id)
                    } label: {
                        Image(systemName: "trash")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(width: 28, height: 28)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("删除听中笔记")
                    .accessibilityIdentifier("player.listenAlong.delete.\(note.id)")
                }
            }

            if !links.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(links) { link in
                        linkRow(link)
                    }
                }
                .padding(.leading, 12)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    @ViewBuilder
    private func linkRow(_ link: ListenAlongLink) -> some View {
        switch link {
        case let .point(_, text, tStart, reason):
            Button {
                onJumpToSeconds(tStart)
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(text)
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    Text(reason)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("player.listenAlong.link.\(link.id)")
        case let .listenNote(_, summary, reason):
            Button {
                onScrollToListenNote()
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text("本期听后感 · \(summary)")
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    Text(reason)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("player.listenAlong.link.\(link.id)")
        }
    }

    private func saveCapture() {
        let position = capturedPosition ?? (player.current.isFinite ? max(0, player.current) : 0)
        guard listenAlongNotes.add(
            mediaKey: episode.mediaKey,
            episodeId: episode.id,
            position: position,
            text: draftText
        ) != nil else {
            saveFailed = true
            return
        }
        cancelCapture()
    }

    private func cancelCapture() {
        draftText = ""
        capturedPosition = nil
        saveFailed = false
        isCapturing = false
        captureFocused = false
    }
}
