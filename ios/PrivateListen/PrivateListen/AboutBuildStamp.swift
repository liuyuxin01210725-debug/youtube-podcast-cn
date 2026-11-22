import SwiftUI

enum BuildStamp {
    static var gitShortSHA: String? { cleaned(Bundle.main.object(forInfoDictionaryKey: "GIT_SHORT_SHA") as? String) }
    static var buildTimestamp: String? { cleaned(Bundle.main.object(forInfoDictionaryKey: "BUILD_TIMESTAMP") as? String) }

    private static func cleaned(_ raw: String?) -> String? {
        let trimmed = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        if trimmed.hasPrefix("$(") && trimmed.hasSuffix(")") { return nil }
        if trimmed == "UNKNOWN" || trimmed == "unknown" { return nil }
        return trimmed
    }
}

/// Read-only Settings/About footer. Shows only the injected short SHA and
/// wall-clock build timestamp. Missing values stay visibly missing; they are
/// never replaced with a ready-looking placeholder.
struct AboutBuildStampView: View {
    var body: some View {
        VStack(spacing: 8) {
            Spacer()
            Text(BuildStamp.gitShortSHA.map { "版本 \($0)" } ?? "版本未注入")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .accessibilityIdentifier("about.git-short-sha")
            Text(BuildStamp.buildTimestamp ?? "构建时间未注入")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .accessibilityIdentifier("about.build-timestamp")
        }
        .frame(maxWidth: .infinity)
        .padding()
        .navigationTitle("关于")
        .navigationBarTitleDisplayMode(.inline)
    }
}
