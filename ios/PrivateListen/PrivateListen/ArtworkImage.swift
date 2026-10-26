import MediaPlayer
import UIKit

enum ArtworkImage {
    static func nowPlayingItem(for episode: Episode) -> MPMediaItemArtwork {
        let image = uiImage(for: episode)
        return MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }

    static func uiImage(for episode: Episode) -> UIImage {
        if episode.artworkKind != "placeholder",
           let url = episode.artworkURL,
           LocalAudio.isPlayable(url),
           let image = UIImage(contentsOfFile: url.path),
           image.size.width > 1 {
            return image
        }
        if let url = episode.artworkURL, episode.artworkKind != "placeholder" {
            try? FileManager.default.startDownloadingUbiquitousItem(at: url)
        }
        return generated(from: episode.title)
    }

    static func generated(from title: String) -> UIImage {
        let size = CGSize(width: 600, height: 600)
        let renderer = UIGraphicsImageRenderer(size: size)
        let parts = title.components(separatedBy: " · ")
        let topic = parts.first?.trimmingCharacters(in: .whitespacesAndNewlines)
        let show = parts.count > 1 ? parts.dropFirst().joined(separator: " · ") : "私人听"
        return renderer.image { _ in
            color(for: title).setFill()
            UIBezierPath(rect: CGRect(origin: .zero, size: size)).fill()
            let topicStyle = NSMutableParagraphStyle()
            topicStyle.alignment = .center
            let topicText = (topic?.isEmpty == false ? topic! : "私人听") as NSString
            let topicRect = CGRect(x: 48, y: 180, width: 504, height: 220)
            topicText.draw(in: topicRect, withAttributes: [
                .font: UIFont.systemFont(ofSize: 44, weight: .semibold),
                .foregroundColor: UIColor.white,
                .paragraphStyle: topicStyle,
            ])
            let showStyle = NSMutableParagraphStyle()
            showStyle.alignment = .center
            (show as NSString).draw(in: CGRect(x: 48, y: 420, width: 504, height: 80), withAttributes: [
                .font: UIFont.systemFont(ofSize: 24, weight: .regular),
                .foregroundColor: UIColor.white.withAlphaComponent(0.85),
                .paragraphStyle: showStyle,
            ])
        }
    }

    private static func color(for title: String) -> UIColor {
        var hash: UInt32 = 2166136261
        for byte in title.utf8 {
            hash ^= UInt32(byte)
            hash &*= 16777619
        }
        let red = CGFloat(24 + (hash & 0x1F)) / 255
        let green = CGFloat(36 + ((hash >> 5) & 0x2F)) / 255
        let blue = CGFloat(48 + ((hash >> 10) & 0x37)) / 255
        return UIColor(red: red, green: green, blue: blue, alpha: 1)
    }
}
