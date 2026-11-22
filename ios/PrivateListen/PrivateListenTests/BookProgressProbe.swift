import Foundation
@main struct BookProgressProbe {
    static func main() {
        let r = BookPlaybackReference(bookID: "book-a", packageRevision: "r1", chapterID: "c1", audioRevision: "audio1")
        var mark = BookMark(reference: r, bookTitle: "A", chapterTitle: "One")
        mark = BookProgressPolicy.record(mark, seconds: 3.5, duration: 20, playedDelta: 0.5, ended: false, at: 12)
        precondition(mark.seconds == 3.5 && mark.lastActuallyListenedAt == 12 && !mark.finished)
        let paused = BookProgressPolicy.record(mark, seconds: 4, duration: 20, playedDelta: 0, ended: false, at: 20)
        precondition(paused.lastActuallyListenedAt == 12)
        let skipped = BookProgressPolicy.record(mark, seconds: 20, duration: 20, playedDelta: 0, ended: true, at: 21)
        precondition(!skipped.finished)
        precondition(BookProgressPolicy.actualAdvance(previous: 4, current: 18, playing: true, rate: 1) == 0)
        precondition(BookProgressPolicy.actualAdvance(previous: nil, current: 18, playing: true, rate: 1) == 0)
        precondition(BookProgressPolicy.actualAdvance(previous: 3, current: 3.5, playing: false, rate: 1) == 0)
        precondition(BookProgressPolicy.actualAdvance(previous: 3, current: 3.5, playing: true, rate: 1) == 0.5)
        precondition(BookProgressPolicy.merging([.init(start: 1,end: 3),.init(start: 2,end: 4)]).count == 1)
        precondition(BookProgressPolicy.bookWins(bookTouch: 30, podcastTouch: 29))
        precondition(!BookProgressPolicy.bookWins(bookTouch: 30, podcastTouch: 31))
        precondition(!BookProgressPolicy.bookWins(bookTouch: nil, podcastTouch: 31))
        let changed = BookPlaybackReference(bookID: "book-a", packageRevision: "r2", chapterID: "c1", audioRevision: "audio2")
        precondition(r.key != changed.key)
        var heard = BookMark(reference: r, bookTitle: "A", chapterTitle: "One")
        for i in 1...40 { heard = BookProgressPolicy.record(heard, seconds: Double(i)/2, duration: 20, playedDelta: 0.5, ended: i == 40, at: Double(i)) }
        precondition(heard.finished)
        var fast = BookMark(reference: r, bookTitle: "A", chapterTitle: "One")
        var previous: Double? = nil
        for second in 1...9 {
            let clock = Double(second)
            let delta = BookProgressPolicy.actualAdvance(previous: previous, current: clock, playing: true, rate: 2)
            fast = BookProgressPolicy.record(fast, seconds: clock, duration: 10, playedDelta: delta, ended: false, at: clock)
            previous = clock
        }
        let tail = BookProgressPolicy.actualAdvance(previous: previous, current: 10, playing: true, rate: 2)
        fast = BookProgressPolicy.record(fast, seconds: 10, duration: 10, playedDelta: tail, ended: true, at: 10)
        precondition(fast.finished, "2x natural EOF must retain the final played fragment")
        print("BookProgressProbe PASS: short listen, one Continue winner, no seek/note touch, distinct revisions, coverage-based EOF")
    }
}
