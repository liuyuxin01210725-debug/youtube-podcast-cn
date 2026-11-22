#if DEBUG
import XCTest

final class LibrarySnapshotTests: XCTestCase {
    func testReadAvailabilityKeepsPublishingStrict() async throws {
        try await MainActor.run {
            try LibrarySnapshotChecks.runReadAvailability()
        }
    }

    func testSnapshotAndLocalCommit() async throws {
        try await MainActor.run {
            try LibrarySnapshotChecks.run()
        }
    }
}
#endif
