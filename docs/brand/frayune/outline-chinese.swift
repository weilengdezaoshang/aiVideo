import Foundation
import CoreText
import CoreGraphics
let url = URL(fileURLWithPath: "/tmp/frayune-noto-medium.otf")
let provider = CGDataProvider(url: url as CFURL)!
let graphicsFont = CGFont(provider)!
let font = CTFontCreateWithGraphicsFont(graphicsFont, 24, nil, nil)
var chars = Array("帧屿集".utf16)
var glyphs = Array(repeating: CGGlyph(0), count: chars.count)
CTFontGetGlyphsForCharacters(font, &chars, &glyphs, chars.count)
var output = ""
for (index, glyph) in glyphs.enumerated() {
 let path = CTFontCreatePathForGlyph(font, glyph, nil)!
 var commands = ""
 path.applyWithBlock { ptr in
  let e = ptr.pointee
  func point(_ n: Int) -> String { let p=e.points[n]; return String(format:"%.3f %.3f", p.x + Double(index)*32, 26-p.y) }
  switch e.type {
  case .moveToPoint: commands += "M" + point(0)
  case .addLineToPoint: commands += "L" + point(0)
  case .addQuadCurveToPoint: commands += "Q" + point(0) + " " + point(1)
  case .addCurveToPoint: commands += "C" + point(0) + " " + point(1) + " " + point(2)
  case .closeSubpath: commands += "Z"
  @unknown default: break
  }
 }
 output += "<path d=\"" + commands + "\"/>"
}
try output.write(toFile:"/tmp/frayune-cn-paths.txt", atomically:true, encoding:.utf8)
