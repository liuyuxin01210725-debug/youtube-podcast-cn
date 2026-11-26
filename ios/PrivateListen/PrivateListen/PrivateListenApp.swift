import SwiftUI

@main
struct PrivateListenApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var library = LibraryStore()
    @StateObject private var books = BookStore()
    @StateObject private var player = PlayerModel()
    @StateObject private var extracts = ExtractStore()
    @StateObject private var pointStars = PointStarStore()
    @StateObject private var listenNotes = ListenNoteStore()
    @StateObject private var listenAlongNotes = ListenAlongNoteStore()

    var body: some Scene {
        WindowGroup {
            // Option A: single primary surface = 资料库 (chips filter owned content).
            // Discover mall tab removed; lenses appear as progressive Library chips.
            // DiscoverView.swift / DiscoverCatalog.swift remain in the target unused by TabView.
            Group {
                #if DEBUG && targetEnvironment(simulator)
                if !DebugSyntheticEpisodeRoute.requested(), !DebugBilingualFixtureRoute.requested(), !DebugListenPicksFixtureRoute.requested(), PointListenProbe.previewEnabled {
                    PointListenPreview()
                } else {
                    LibraryView()
                }
                #else
                LibraryView()
                #endif
            }
                .environmentObject(library)
                .environmentObject(books)
                .environmentObject(player)
                .environmentObject(extracts)
                .environmentObject(pointStars)
                .environmentObject(listenNotes)
                .environmentObject(listenAlongNotes)
                .task {
                    #if DEBUG
                    guard !DebugSyntheticEpisodeRoute.requested(), !DebugBilingualFixtureRoute.requested(), !DebugListenPicksFixtureRoute.requested() else { return }
                    #endif
                    #if DEBUG && !targetEnvironment(simulator)
                    await NortonDeviceFourProbe.runIfNeeded(player: player, library: library)
                    #endif
                    #if DEBUG && targetEnvironment(simulator)
                    await PointListenProbe.runIfNeeded(player: player, library: library)
                    #endif
                }
                .onOpenURL { url in
                    library.acceptIncoming(url)
                }
                .onChange(of: scenePhase) { _, phase in
                    guard phase == .active else { return }
                    library.refreshLocalLibraryOnActivation()
                }
        }
    }
}
