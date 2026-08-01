import AppKit
import CoreImage
import Foundation
import ImageIO
import simd
import Vision

private let payloadPrefix = "AMSVC:"
private let markerIDs = ["TL", "TR", "BR", "BL"]

private func qrImage(_ value: String, side: CGFloat) -> NSImage? {
    guard let filter = CIFilter(name: "CIQRCodeGenerator"),
          let data = value.data(using: .utf8) else { return nil }
    filter.setValue(data, forKey: "inputMessage")
    filter.setValue("H", forKey: "inputCorrectionLevel")
    guard let output = filter.outputImage else { return nil }
    let scale = side / output.extent.width
    let scaled = output.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
    let image = NSImage(size: scaled.extent.size)
    image.addRepresentation(NSCIImageRep(ciImage: scaled))
    return image
}

private final class CalibrationSheetView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let page = bounds.size
    NSColor.white.setFill()
    NSBezierPath(rect: NSRect(origin: .zero, size: page)).fill()

    let mm: CGFloat = 72.0 / 25.4
    let plateSide: CGFloat = 180 * mm
    let origin = NSPoint(x: (page.width - plateSide) / 2, y: 84 * mm)
    let title = "AMS Lite Companion — calibration Vision"
    title.draw(at: NSPoint(x: 46 * mm, y: 270 * mm), withAttributes: [
        .font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.black,
    ])
    let instructions = "Imprimez à 100 % (sans ajustement), découpez le carré, puis posez-le à plat sur le plateau vide."
    instructions.draw(at: NSPoint(x: 24 * mm, y: 260 * mm), withAttributes: [
        .font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.darkGray,
    ])
    NSColor.black.setStroke()
    let plate = NSRect(origin: origin, size: NSSize(width: plateSide, height: plateSide))
    let border = NSBezierPath(rect: plate)
    border.lineWidth = 1.5
    border.stroke()

    // Centres are 20 mm in from each plate edge; their positions are the
    // world-coordinate correspondences used by the local homography.
    let positions: [String: NSPoint] = [
        "TL": NSPoint(x: 20, y: 160), "TR": NSPoint(x: 160, y: 160),
        "BR": NSPoint(x: 160, y: 20), "BL": NSPoint(x: 20, y: 20),
    ]
    let markerSide: CGFloat = 18 * mm
    for id in markerIDs {
        guard let point = positions[id], let qr = qrImage(payloadPrefix + id, side: markerSide) else { continue }
        let center = NSPoint(x: origin.x + point.x * mm, y: origin.y + point.y * mm)
        let rect = NSRect(x: center.x - markerSide / 2, y: center.y - markerSide / 2,
                          width: markerSide, height: markerSide)
        NSGraphicsContext.current?.imageInterpolation = .none
        qr.draw(in: rect)
        let label = id == "TL" ? "X0 / Y0" : id == "TR" ? "Xmax / Y0" : id == "BR" ? "Xmax / Ymax" : "X0 / Ymax"
        label.draw(at: NSPoint(x: rect.minX, y: rect.maxY + 3), withAttributes: [
            .font: NSFont.boldSystemFont(ofSize: 8), .foregroundColor: NSColor.black,
        ])
    }
    "180 mm × 180 mm — ne modifie jamais l’imprimante".draw(
        at: NSPoint(x: origin.x, y: origin.y - 8 * mm), withAttributes: [
            .font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.darkGray,
        ])
    }
}

private func sheetPDF() -> Data? {
    let page = NSSize(width: 595.28, height: 841.89) // A4 in PostScript points.
    let view = CalibrationSheetView(frame: NSRect(origin: .zero, size: page))
    return view.dataWithPDF(inside: view.bounds)
}

private func imageAtPath(_ path: String) -> CGImage? {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

private func markerDetections(path: String) throws -> [[String: Any]] {
    guard let image = imageAtPath(path) else { throw NSError(domain: "AMSVC", code: 1) }
    let request = VNDetectBarcodesRequest()
    request.symbologies = [.QR]
    let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
    try handler.perform([request])
    var result: [[String: Any]] = []
    var seen = Set<String>()
    for observation in request.results ?? [] {
        guard let text = observation.payloadStringValue,
              text.hasPrefix(payloadPrefix) else { continue }
        let id = String(text.dropFirst(payloadPrefix.count))
        guard markerIDs.contains(id), !seen.contains(id) else { continue }
        seen.insert(id)
        let box = observation.boundingBox
        // Vision uses a lower-left origin; the HTML image uses upper-left.
        result.append(["id": id, "x": box.midX, "y": 1.0 - box.midY])
    }
    return result
}

private func registration(referencePath: String, currentPath: String) throws -> [String: Any] {
    guard let reference = imageAtPath(referencePath), let current = imageAtPath(currentPath) else {
        throw NSError(domain: "AMSVC", code: 4)
    }
    // The targeted image is the moving (current) frame. Vision returns the
    // warp from that frame back to the fixed reference, so we invert it before
    // returning a transform from reference pixels to current pixels.
    let request = VNHomographicImageRegistrationRequest(targetedCGImage: current, options: [:])
    let handler = VNImageRequestHandler(cgImage: reference, orientation: .up, options: [:])
    try handler.perform([request])
    guard let observation = request.results?.first else {
        return ["registered": false, "message": "Aucun recalage trouvé"]
    }
    let transform = simd_inverse(observation.warpTransform)
    guard transform.columns.2.z.isFinite, abs(transform.columns.2.z) > 0.000001 else {
        return ["registered": false, "message": "Transformation de recalage invalide"]
    }
    let referenceScale = matrix_float3x3(
        SIMD3<Float>(Float(reference.width), 0, 0),
        SIMD3<Float>(0, Float(reference.height), 0),
        SIMD3<Float>(0, 0, 1)
    )
    let currentScaleInverse = matrix_float3x3(
        SIMD3<Float>(1 / Float(current.width), 0, 0),
        SIMD3<Float>(0, 1 / Float(current.height), 0),
        SIMD3<Float>(0, 0, 1)
    )
    // Vision coordinates are lower-left. Convert them to the upper-left
    // normalized coordinates used by the local HTML canvas: F * H * F.
    let flip = matrix_float3x3(
        SIMD3<Float>(1, 0, 0),
        SIMD3<Float>(0, -1, 0),
        SIMD3<Float>(0, 1, 1)
    )
    let unnormalizedBrowser = flip * currentScaleInverse * transform * referenceScale * flip
    let browser = unnormalizedBrowser * (1 / unnormalizedBrowser.columns.2.z)
    let values: [[Float]] = [
        [browser.columns.0.x, browser.columns.1.x, browser.columns.2.x],
        [browser.columns.0.y, browser.columns.1.y, browser.columns.2.y],
        [browser.columns.0.z, browser.columns.1.z, browser.columns.2.z],
    ]
    guard values.flatMap({ $0 }).allSatisfy({ $0.isFinite }) else {
        return ["registered": false, "message": "Transformation de recalage non finie"]
    }
    return ["registered": true, "matrix": values]
}

private func run() throws {
    let arguments = CommandLine.arguments
    if arguments.count == 2 && arguments[1] == "--sheet" {
        guard let data = sheetPDF() else { throw NSError(domain: "AMSVC", code: 2) }
        FileHandle.standardOutput.write(data)
        return
    }
    if arguments.count == 3 && arguments[1] == "--detect" {
        let markers = try markerDetections(path: arguments[2])
        let data = try JSONSerialization.data(withJSONObject: ["markers": markers], options: [])
        FileHandle.standardOutput.write(data)
        return
    }
    if arguments.count == 4 && arguments[1] == "--register" {
        let result = try registration(referencePath: arguments[2], currentPath: arguments[3])
        let data = try JSONSerialization.data(withJSONObject: result, options: [])
        FileHandle.standardOutput.write(data)
        return
    }
    throw NSError(domain: "AMSVC", code: 64)
}

do {
    try run()
} catch {
    fputs("AMS Vision calibration helper error: \(error)\n", stderr)
    exit(1)
}
