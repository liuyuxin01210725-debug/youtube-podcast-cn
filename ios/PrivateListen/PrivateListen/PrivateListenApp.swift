import SwiftUI

@main
struct PrivateListenApp: App {
    @StateObject private var library = LibraryStore()
    @StateObject private var player = PlayerModel()

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environmentObject(library)
                .environmentObject(player)
        }
    }
}
