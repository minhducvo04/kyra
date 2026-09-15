// Run from the repository root: swift apple/Design/generate_app_icon.swift
// Original vector artwork. visionOS supplies the circular mask and layer depth.
import AppKit

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let catalog = root.appendingPathComponent("apple/KyraVision/KyraVision/Assets.xcassets")
let stack = catalog.appendingPathComponent("AppIcon.solidimagestack")
let info: [String: Any] = ["author": "xcode", "version": 1]
let side = 1024
let space = CGColorSpace(name: CGColorSpace.sRGB)!

func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: space, components: [r, g, b, a])!
}
func metadata(_ value: [String: Any], at url: URL) throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]).write(to: url)
}
func canvas() -> CGContext {
    CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
              space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
}
func radial(_ context: CGContext, colors: [CGColor], locations: [CGFloat],
            from start: CGPoint, to end: CGPoint, radius: CGFloat) {
    let gradient = CGGradient(colorsSpace: space, colors: colors as CFArray, locations: locations)!
    context.drawRadialGradient(gradient, startCenter: start, startRadius: 0,
                               endCenter: end, endRadius: radius,
                               options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
}
func png(_ context: CGContext, at url: URL) throws {
    let image = NSBitmapImageRep(cgImage: context.makeImage()!)
    try image.representation(using: .png, properties: [:])!.write(to: url)
}

let back = canvas()
radial(back, colors: [color(0.08, 0.25, 0.34), color(0.025, 0.07, 0.15), color(0.018, 0.035, 0.09)],
       locations: [0, 0.65, 1], from: CGPoint(x: 450, y: 600), to: CGPoint(x: 512, y: 512), radius: 700)

let middle = canvas()
middle.saveGState()
middle.addEllipse(in: CGRect(x: 206, y: 206, width: 612, height: 612))
middle.clip()
radial(middle, colors: [color(0.87, 1, 1), color(0.40, 0.95, 0.98), color(0.10, 0.68, 0.81),
                        color(0.08, 0.32, 0.56), color(0.07, 0.11, 0.27)],
       locations: [0, 0.19, 0.45, 0.76, 1], from: CGPoint(x: 370, y: 700),
       to: CGPoint(x: 510, y: 505), radius: 410)
middle.restoreGState()
middle.setStrokeColor(color(0.63, 0.94, 1, 0.55))
middle.setLineWidth(2)
middle.strokeEllipse(in: CGRect(x: 207, y: 207, width: 610, height: 610))

let front = canvas()
// An open orbit echoes Kyra's existing presence rings and remains legible when small.
front.saveGState()
front.translateBy(x: 512, y: 512)
front.rotate(by: -.pi / 7)
front.scaleBy(x: 1, y: 0.86)
front.addArc(center: .zero, radius: 368, startAngle: .pi * 0.15,
             endAngle: .pi * 1.55, clockwise: false)
front.setStrokeColor(color(0.67, 0.96, 1, 0.92))
front.setLineWidth(12)
front.setLineCap(.round)
front.strokePath()
front.restoreGState()

try metadata(["info": info], at: catalog.appendingPathComponent("Contents.json"))
try metadata(["info": info, "layers": ["Front", "Middle", "Back"].map {
    ["filename": "\($0).solidimagestacklayer"]
}], at: stack.appendingPathComponent("Contents.json"))
for (name, context) in [("Back", back), ("Middle", middle), ("Front", front)] {
    let layer = stack.appendingPathComponent("\(name).solidimagestacklayer")
    let content = layer.appendingPathComponent("Content.imageset")
    try metadata(["info": info], at: layer.appendingPathComponent("Contents.json"))
    try metadata(["info": info, "images": [["idiom": "vision", "scale": "2x", "filename": "\(name).png"]]],
                 at: content.appendingPathComponent("Contents.json"))
    try png(context, at: content.appendingPathComponent("\(name).png"))
}
// Flat preview is design documentation only; the app consumes the three layers above.
let preview = canvas()
for layer in [back, middle, front] {
    preview.draw(layer.makeImage()!, in: CGRect(x: 0, y: 0, width: side, height: side))
}
try png(preview, at: root.appendingPathComponent("apple/Design/KyraIcon-preview.png"))
print("Generated three sRGB icon layers and a composite preview.")
