import Foundation
import AVFoundation

/// Real muted macOS AVPlayer + BookStore/BookProgressPolicy. Not the iOS PlayerModel/UI.
@main struct BookMediaPreinstallJourney {
    struct Failure: Error { var reason:String }
    @MainActor static func main() async throws {
        let mode=CommandLine.arguments[1], source=URL(fileURLWithPath:CommandLine.arguments[2])
        let docs=URL(fileURLWithPath:CommandLine.arguments[3],isDirectory:true)
        let store=BookStore(documents:docs)
        if mode=="seed" { await store.importPackage(source) }
        guard let e=store.editions.first else { throw Failure(reason:store.errorText ?? "no edition") }
        let chapters=e.manifest.chapters.sorted{$0.order<$1.order};let a=chapters[0],b=chapters[1]
        let ref=e.reference(a)!
        func check(_ value:Bool,_ message:String)throws { guard value else {throw Failure(reason:message)};print("PASS "+message) }
        @MainActor func play(_ chapter:BookChapter, from start:Double, rate:Float, stopAfter:Double? = nil, book:Bool=true) async throws -> Double {
            let url=try store.audioURL(edition:e,chapter:chapter)
            let asset=AVURLAsset(url:url);let time=try await asset.load(.duration);let length=time.seconds
            let item=AVPlayerItem(asset:asset),player=AVPlayer();player.isMuted=true;player.replaceCurrentItem(with:item)
            let readyDeadline=Date().addingTimeInterval(10)
            while item.status == .unknown && Date()<readyDeadline { try await Task.sleep(nanoseconds:100_000_000) }
            guard item.status == .readyToPlay else {throw Failure(reason:"real AVPlayer failed to become ready: "+(item.error?.localizedDescription ?? "timeout"))}
            if start>0 {
                let finished=await withCheckedContinuation { c in player.seek(to:CMTime(seconds:start,preferredTimescale:600),toleranceBefore:.zero,toleranceAfter:.zero){c.resume(returning:$0)} }
                try check(finished && abs(player.currentTime().seconds-start)<0.5,"real seek lands at persisted resume")
            }
            var previous:Double?=player.currentTime().seconds
            let reference=e.reference(chapter)!, deadline=Date().addingTimeInterval(length/Double(rate)+12)
            player.playImmediately(atRate:rate)
            var last=previous ?? 0
            while Date()<deadline {
                try await Task.sleep(nanoseconds:100_000_000)
                let clock=player.currentTime().seconds
                if !clock.isFinite {continue}
                let ended=clock>=length-0.06 && player.rate==0
                let delta=BookProgressPolicy.actualAdvance(previous:previous,current:clock,playing:player.timeControlStatus == .playing || ended,rate:rate)
                if delta>0 && book { store.remember(reference,seconds:clock,duration:length,playedDelta:delta,ended:ended) }
                if player.timeControlStatus == .playing {previous=clock}
                last=clock
                if let limit=stopAfter, clock-start>=limit {player.pause();print("TRANSPORT chapter=\(chapter.id) actual=\(clock) rate=\(rate) muted=true stopped=short");return clock}
                if ended {player.pause();if book {store.remember(reference,seconds:clock,duration:length,playedDelta:0,ended:true)};print("TRANSPORT chapter=\(chapter.id) actual=\(clock) duration=\(length) rate=\(rate) muted=true naturalEOF=true");return clock}
                if item.status == .failed {throw Failure(reason:item.error?.localizedDescription ?? "transport failed")}
            }
            player.pause();throw Failure(reason:"real AVPlayer bounded deadline exceeded at \(last)")
        }
        if mode=="seed" {
            let clock=try await play(a,from:0,rate:1,stopAfter:3.6)
            store.saveNote(bookID:e.manifest.bookId,revision:e.manifest.packageRevision,chapterID:a.id,text:String(repeating:"跨进程保留听后感。",count:1000))
            try check(clock>=3 && clock<6 && store.resume(ref)>=3,"short real playback persisted before process exit")
            print("SEED process may now exit; no API/model/TTS call")
        } else {
            let resume=store.resume(ref)
            try check(resume>=3 && resume<6,"new process reloads persisted resume before opening player")
            try check(store.note(bookID:e.manifest.bookId,revision:e.manifest.packageRevision,chapterID:a.id)?.body==String(repeating:"跨进程保留听后感。",count:1000),"long note survives actual process boundary")
            _=try await play(a,from:resume,rate:2)
            try check(store.state.marks[ref.key]?.finished==true,"real 2x natural chapter EOF records completion")
            _=try await play(b,from:0,rate:2)
            try check(store.heard(e),"chapter switch retains A completion and completes B")
            // Self-authored MP3 is the podcast transport fixture, not an old/private episode.
            _=try await play(a,from:0,rate:1,stopAfter:3.2,book:false)
            let podcastTouch=Date().timeIntervalSince1970
            let podcast=ContinueListening.items(marks:["podcast-fixture":.init(seconds:3.2,finished:false,touchedAt:podcastTouch)],durations:["podcast-fixture":a.audio!.durationSeconds],dismissed:[])
            try check(podcast.count==1 && !BookProgressPolicy.bookWins(bookTouch:store.continueMark?.lastActuallyListenedAt,podcastTouch:podcast.first?.touchedAt),"podcast return wins shared pure Continue policy")
            _=try await play(a,from:0,rate:2,stopAfter:3.2)
            try check(BookProgressPolicy.bookWins(bookTouch:store.continueMark?.lastActuallyListenedAt,podcastTouch:podcastTouch),"real book replay wins shared Continue policy again")
            print("RESUME/SWITCH PASS: muted real AVPlayer clock + real store disk IO + policy; not iOS App UI, human listening or physical-device proof")
        }
    }
}
