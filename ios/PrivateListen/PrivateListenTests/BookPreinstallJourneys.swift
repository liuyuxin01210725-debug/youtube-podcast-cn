import Foundation

/// Executes real BookStore disk IO against authorized copied packages; no App/UI claim.
@main struct BookPreinstallJourneys {
    @MainActor static func main() async throws {
        let source=URL(fileURLWithPath:CommandLine.arguments[1],isDirectory:true)
        let draft=URL(fileURLWithPath:CommandLine.arguments[2],isDirectory:true)
        let root=URL(fileURLWithPath:CommandLine.arguments[3],isDirectory:true)
        let fm=FileManager.default
        try fm.createDirectory(at:root,withIntermediateDirectories:true)
        var failures=0; var checks=0
        func check(_ ok:Bool,_ name:String) { checks += 1; if !ok { failures += 1 }; print("\(ok ? "PASS" : "FAIL") \(name)") }
        func variant(_ name:String,_ change:(inout [String:Any],URL)throws->Void)throws->URL {
            let u=root.appendingPathComponent("inputs/"+name)
            try fm.createDirectory(at:u.deletingLastPathComponent(),withIntermediateDirectories:true)
            try fm.copyItem(at:source,to:u)
            var m=try JSONSerialization.jsonObject(with:Data(contentsOf:u.appendingPathComponent("manifest.json"))) as! [String:Any]
            try change(&m,u)
            try JSONSerialization.data(withJSONObject:m,options:[.sortedKeys]).write(to:u.appendingPathComponent("manifest.json"))
            return u
        }
        let docs=root.appendingPathComponent("documents")
        let store=BookStore(documents:docs)
        await store.importPackage(source)
        check(store.errorText==nil && store.editions.count==1,"R1 real authorized package import")
        let edition=store.editions.first!;let chapters=edition.manifest.chapters.sorted{$0.order<$1.order}
        check(chapters.count==2 && chapters[0].title.contains("第一章") && chapters[1].title.contains("第二章"),"R1 spine chapter order, not nav order")
        let ref=edition.reference(chapters[0])!
        store.remember(ref,seconds:4,duration:chapters[0].audio!.durationSeconds,playedDelta:0.5,ended:false)
        let long=String(repeating:"保留长听后感：北三南零，没有施肥，注释不是预测。\n",count:1000)
        store.saveNote(bookID:edition.manifest.bookId,revision:edition.manifest.packageRevision,chapterID:chapters[0].id,text:long)
        let noteURL=docs.appendingPathComponent("book_notes.v1.json");let notesBefore=try Data(contentsOf:noteURL)
        await store.importPackage(source)
        check(try store.errorText==nil && store.editions.count==1 && (Data(contentsOf:noteURL))==notesBefore,"R1 duplicate import idempotent, long note bytes unchanged")
        let corrupt=try variant("corrupt") { m,u in
            m["book_id"]="book-corrupt"
            let c=(m["chapters"] as! [[String:Any]])[0];let a=c["audio"] as! [String:Any]
            try Data("not the original audio".utf8).write(to:u.appendingPathComponent(a["path"] as! String))
        }
        await store.importPackage(corrupt)
        check(store.errorText != nil && store.editions.count==1 && !fm.fileExists(atPath:docs.appendingPathComponent("Books/book-corrupt").path),"R1 corrupt asset fails before publication")
        check((try Data(contentsOf:noteURL))==notesBefore,"R1 failure preserves old long notes")
        await store.importPackage(source)
        check(store.errorText==nil,"R1 valid import after failure clears stale error")
        let collision=try variant("collision") { m,_ in m["title"]="same revision conflicting metadata" }
        await store.importPackage(collision)
        check(store.errorText != nil && store.editions.first!.manifest.title==edition.manifest.title,"R1 same-revision collision rejected, original preserved")
        let traversal=try variant("traversal") { m,_ in
            m["book_id"]="book-traversal";var assets=m["assets"] as! [String:Any]
            assets["../escape"]=["sha256":String(repeating:"0",count:64),"bytes":0];m["assets"]=assets
        }
        await store.importPackage(traversal)
        check(store.errorText != nil && !fm.fileExists(atPath:docs.appendingPathComponent("escape").path),"R1 traversal rejected")
        await store.importPackage(source)
        await store.importPackage(draft)
        check(store.state.selected[edition.manifest.bookId]==edition.manifest.packageRevision,"R3 new extraction does not replace selected edition")
        check((try Data(contentsOf:noteURL))==notesBefore,"R3 new revision preserves long-note bytes")
        let reopened=BookStore(documents:docs)
        check(reopened.resume(ref)==4 && reopened.note(bookID:edition.manifest.bookId,revision:edition.manifest.packageRevision,chapterID:chapters[0].id)?.body==long,"R3 store restart restores exact typed progress and long text")
        let saved=root.appendingPathComponent("saved-original-edition")
        try fm.moveItem(at:edition.root,to:saved);reopened.reload()
        check(!reopened.selectedEditions.contains{$0.manifest.bookId==edition.manifest.bookId},"R3 unavailable selected revision must not silently show another edition")
        check(reopened.errorText != nil,"R3 unavailable revision has visible warning")
        check(try reopened.resume(ref)==4 && (Data(contentsOf:noteURL))==notesBefore,"R3 unavailable revision retains original progress and notes")
        try fm.moveItem(at:saved,to:edition.root);reopened.reload()
        check(reopened.selectedEditions.contains{$0.manifest.packageRevision==edition.manifest.packageRevision},"R3 restored revision usable without migration")
        let audio=try reopened.audioURL(edition:edition,chapter:chapters[0]);let bytes=try Data(contentsOf:audio)
        var damaged=bytes;damaged[0] ^= 1;try damaged.write(to:audio)
        do { _=try reopened.audioURL(edition:edition,chapter:chapters[0]);check(false,"R3 changed audio is refused") } catch { check(true,"R3 changed audio is refused") }
        check(reopened.resume(ref)==4,"R3 refused audio does not migrate/reset position")
        try bytes.write(to:audio)
        let podcastTouch=(reopened.continueMark?.lastActuallyListenedAt ?? 0)+1
        check(!BookProgressPolicy.bookWins(bookTouch:reopened.continueMark?.lastActuallyListenedAt,podcastTouch:podcastTouch),"R2 podcast later touch wins unique Continue policy")
        reopened.remember(ref,seconds:4.5,duration:chapters[0].audio!.durationSeconds,playedDelta:0.5,ended:false)
        check(BookProgressPolicy.bookWins(bookTouch:reopened.continueMark?.lastActuallyListenedAt,podcastTouch:0),"R2 actual book advance can win Continue")
        let touched=reopened.continueMark?.lastActuallyListenedAt
        reopened.saveNote(bookID:edition.manifest.bookId,revision:edition.manifest.packageRevision,chapterID:chapters[0].id,text:long)
        check(reopened.continueMark?.lastActuallyListenedAt==touched,"R2 writing note does not steal Continue")
        let unsupported=root.appendingPathComponent("unsupported-documents")
        try fm.createDirectory(at:unsupported,withIntermediateDirectories:true)
        let unknown=Data("{\"schemaVersion\":99,\"selected\":{},\"marks\":{},\"dismissed\":[]}".utf8)
        try unknown.write(to:unsupported.appendingPathComponent("book_state.v1.json"))
        let unknownNotes=Data("old malformed notes must survive".utf8);try unknownNotes.write(to:unsupported.appendingPathComponent("book_notes.v1.json"))
        let blocked=BookStore(documents:unsupported);blocked.select(edition);blocked.saveNote(bookID:edition.manifest.bookId,revision:edition.manifest.packageRevision,chapterID:nil,text:"not allowed")
        check(try (Data(contentsOf:unsupported.appendingPathComponent("book_state.v1.json")))==unknown && (Data(contentsOf:unsupported.appendingPathComponent("book_notes.v1.json")))==unknownNotes,"R3 unsupported sidecars never overwritten")
        let concurrent=BookStore(documents:root.appendingPathComponent("concurrent-documents"))
        async let left: Void = concurrent.importPackage(source)
        async let right: Void = concurrent.importPackage(source)
        _ = await (left,right)
        check(concurrent.editions.count==1 && concurrent.errorText==nil && !concurrent.importing,"R1 concurrent duplicate imports serialize/idempotently finish")
        let order=try variant("bad-order") { m,_ in
            m["package_revision"]=String(repeating:"b",count:64)
            var c=m["chapters"] as! [[String:Any]];c[1]["order"]=0;m["chapters"]=c
        }
        let ordered=BookStore(documents:root.appendingPathComponent("order-documents"));await ordered.importPackage(order)
        check(ordered.editions.isEmpty && ordered.errorText != nil,"R1 duplicate chapter order rejected")
        let spoof=try variant("spoof-whole") { m,u in
            m["package_revision"]=String(repeating:"a",count:64);m["whole_book_verified"]=true
            var gates=m["gates"] as! [String:String];gates["audio"]="pass";m["gates"]=gates
            let p=u.appendingPathComponent("listening-review.json");try Data("{}".utf8).write(to:p)
            var assets=m["assets"] as! [String:Any];assets["listening-review.json"]=["sha256":try BookFiles.hash(p),"bytes":2];m["assets"]=assets
        }
        let guardStore=BookStore(documents:root.appendingPathComponent("claim-documents"))
        await guardStore.importPackage(spoof)
        check(guardStore.editions.isEmpty && guardStore.errorText != nil,"R1 empty listening evidence cannot grant whole-book claim")
        print("SUMMARY checks=\(checks) failures=\(failures); real BookStore/BookFiles disk IO + pure Continue policy, not GUI or physical playback")
        if failures>0 { exit(1) }
    }
}
