#if DEBUG
import XCTest

@MainActor
final class LibrarySnapshotTests: XCTestCase {
    func testReadAvailabilityKeepsPublishingStrict() async throws {
        try await LibrarySnapshotChecks.runReadAvailability()
    }

    func testSnapshotAndLocalCommit() async throws {
        try await LibrarySnapshotChecks.run()
    }
}
#endif
