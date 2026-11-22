#if DEBUG
import XCTest

final class LibrarySnapshotTests: XCTestCase {
    func testSnapshotAndLocalCommit() async throws {
        try await MainActor.run {
            try LibrarySnapshotChecks.run()
        }
    }
}
#endif
