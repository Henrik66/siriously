import AppKit

/// The Siriously mark (siriously-mark-a3: an arc over two dots), drawn natively so the menu bar icon is a
/// crisp template image in light and dark mode. Geometry is the SVG's 0…100 viewBox, cropped to the mark.
enum SiriouslyMark {
    enum State { case listening, working, paused }

    private static let crop = CGRect(x: 12, y: 12, width: 76, height: 76)  // viewBox area the icon shows

    /// The arc "M22 46 A30 30 0 0 1 78 46" rotated -12° about (50, 46), as points (SVG coordinates, y down).
    private static let arc: [CGPoint] = {
        let c = CGPoint(x: 50, y: 46 + (900.0 - 784.0).squareRoot())  // centre below the chord
        let a0 = atan2(46 - c.y, 22 - c.x), a1 = atan2(46 - c.y, 78 - c.x)
        let rot = -12 * Double.pi / 180
        return (0...32).map { i in
            let a = a0 + (a1 - a0) * Double(i) / 32
            let p = CGPoint(x: c.x + 30 * cos(a), y: c.y + 30 * sin(a))
            let dx = p.x - 50, dy = p.y - 46
            return CGPoint(x: 50 + cos(rot) * dx - sin(rot) * dy, y: 46 + sin(rot) * dx + cos(rot) * dy)
        }
    }()

    private static func markPath() -> (stroke: NSBezierPath, dots: NSBezierPath) {
        let s = NSBezierPath()
        s.move(to: arc[0])
        arc.dropFirst().forEach { s.line(to: $0) }
        s.lineWidth = 10
        s.lineCapStyle = .round
        s.lineJoinStyle = .round
        let d = NSBezierPath(ovalIn: CGRect(x: 26, y: 60, width: 16, height: 16))
        d.append(NSBezierPath(ovalIn: CGRect(x: 58, y: 60, width: 16, height: 16)))
        return (s, d)
    }

    static func image(_ state: State, size: CGFloat = 18) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { _ in
            guard let ctx = NSGraphicsContext.current else { return false }
            ctx.cgContext.scaleBy(x: size / crop.width, y: size / crop.height)
            ctx.cgContext.translateBy(x: -crop.minX, y: -crop.minY)
            let (stroke, dots) = markPath()
            NSColor.black.set()
            if state == .working {  // filled disc with the mark cut out
                NSBezierPath(ovalIn: crop.insetBy(dx: 1, dy: 1)).fill()
                ctx.compositingOperation = .destinationOut
                stroke.lineWidth = 8
                let t = NSAffineTransform()
                t.translateX(by: 50, yBy: 50); t.scale(by: 0.62); t.translateX(by: -50, yBy: -48)
                stroke.transform(using: t as AffineTransform)
                dots.transform(using: t as AffineTransform)
                stroke.stroke()
                dots.fill()
                return true
            }
            stroke.stroke()
            dots.fill()
            if state == .paused {  // a slash, with a gap cut around it like SF Symbols' *.slash
                let slash = NSBezierPath()
                slash.move(to: CGPoint(x: 20, y: 18))
                slash.line(to: CGPoint(x: 82, y: 80))
                slash.lineCapStyle = .round
                ctx.compositingOperation = .destinationOut
                slash.lineWidth = 14
                slash.stroke()
                ctx.compositingOperation = .sourceOver
                slash.lineWidth = 6
                slash.stroke()
            }
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "Siriously"
        return img
    }
}
