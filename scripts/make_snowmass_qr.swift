import AppKit
import CoreImage
import Foundation

guard CommandLine.arguments.count == 3 else { exit(2) }
let message = Data(CommandLine.arguments[1].utf8)
let output = URL(fileURLWithPath: CommandLine.arguments[2])
guard let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(3) }
filter.setValue(message, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let image = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 12, y: 12)) else { exit(4) }
let representation = NSBitmapImageRep(ciImage: image)
guard let png = representation.representation(using: .png, properties: [:]) else { exit(5) }
try png.write(to: output)
