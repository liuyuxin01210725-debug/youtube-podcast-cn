import SwiftUI

@main
struct PrivateListenApp: App {
    @StateObject private var library = LibraryStore()
    @StateObject private var player = PlayerModel()
    @StateObject private var extracts = ExtractStore()
    @StateObject private var pointStars = PointStarStore()

    var body: some Scene {
        WindowGroup {
            // Option A: single primary surface = 资料库 (chips filter owned content).
            // Discover mall tab removed; lenses appear as progressive Library chips.
            // DiscoverView.swift / DiscoverCatalog.swift remain in the target unused by TabView.
            LibraryView()
                .environmentObject(library)
                .environmentObject(player)
                .environmentObject(extracts)
                .environmentObject(pointStars)
                .onOpenURL { url in
                    library.acceptIncoming(url)
                }
        }
    }
}
