import SwiftUI

@main
struct PrivateListenApp: App {
    @StateObject private var library = LibraryStore()
    @StateObject private var player = PlayerModel()

    var body: some Scene {
        WindowGroup {
            TabView {
                DiscoverView()
                    .tabItem { Label("发现", systemImage: "circle.grid.cross") }
                LibraryView()
                    .tabItem { Label("资料库", systemImage: "books.vertical") }
            }
            .environmentObject(library)
            .environmentObject(player)
            .onOpenURL { url in
                library.acceptIncoming(url)
            }
        }
    }
}
