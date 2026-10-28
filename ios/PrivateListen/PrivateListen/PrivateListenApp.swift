import SwiftUI

@main
struct PrivateListenApp: App {
    @StateObject private var library = LibraryStore()
    @StateObject private var player = PlayerModel()
    @StateObject private var bookResume = BookResumeStore()

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
            .environmentObject(bookResume)
            .onOpenURL { url in
                library.acceptIncoming(url)
            }
        }
    }
}
