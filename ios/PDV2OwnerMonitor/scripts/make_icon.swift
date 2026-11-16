// Gera o ícone do app (1024×1024, OPACO, sem cantos arredondados) com o
// símbolo "P2" da marca PDV2 por CrownTech sobre o azul da marca.
// Uso: swift scripts/make_icon.swift PDV2OwnerMonitor/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let output = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
let size = 1024
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
// noneSkipLast = sem canal alfa (a App Store recusa ícone com transparência).
guard let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                              space: colorSpace, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
    fatalError("Sem contexto gráfico")
}

func rgb(_ hex: UInt32) -> CGColor {
    CGColor(colorSpace: colorSpace, components: [CGFloat((hex >> 16) & 0xFF) / 255,
                                                 CGFloat((hex >> 8) & 0xFF) / 255,
                                                 CGFloat(hex & 0xFF) / 255, 1])!
}

// Fundo: degradê do azul #146BD6 ao azul escuro #0A4599.
let gradient = CGGradient(colorsSpace: colorSpace, colors: [rgb(0x146BD6), rgb(0x0A4599)] as CFArray,
                          locations: [0, 1])!
context.drawLinearGradient(gradient, start: CGPoint(x: 0, y: CGFloat(size)), end: CGPoint(x: CGFloat(size), y: 0),
                           options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])

// Desenha no sistema de coordenadas do SVG da marca (viewBox 64, y para baixo),
// com a mesma transformação translate(5.5 5.5) scale(1.36).
context.translateBy(x: 0, y: CGFloat(size))
context.scaleBy(x: CGFloat(size) / 64, y: -CGFloat(size) / 64)
context.translateBy(x: 5.5, y: 5.5)
context.scaleBy(x: 1.36, y: 1.36)
context.setLineWidth(3.4)
context.setLineCap(.round)
context.setLineJoin(.round)

// "P": M8 28 V8 h11 a7 7 0 0 1 0 14 H8
let p = CGMutablePath()
p.move(to: CGPoint(x: 8, y: 28))
p.addLine(to: CGPoint(x: 8, y: 8))
p.addLine(to: CGPoint(x: 19, y: 8))
p.addArc(center: CGPoint(x: 19, y: 15), radius: 7, startAngle: -.pi / 2, endAngle: .pi / 2, clockwise: false)
p.addLine(to: CGPoint(x: 8, y: 22))
context.addPath(p)
context.setStrokeColor(rgb(0xFFFFFF))
context.strokePath()

// "2": M23 23 c3-4 8-2 8 1 0 3-6 4-8 7 h8
let two = CGMutablePath()
two.move(to: CGPoint(x: 23, y: 23))
two.addCurve(to: CGPoint(x: 31, y: 24), control1: CGPoint(x: 26, y: 19), control2: CGPoint(x: 31, y: 21))
two.addCurve(to: CGPoint(x: 23, y: 31), control1: CGPoint(x: 31, y: 27), control2: CGPoint(x: 25, y: 28))
two.addLine(to: CGPoint(x: 31, y: 31))
context.addPath(two)
context.setStrokeColor(rgb(0x2EBF94))
context.strokePath()

guard let image = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: output) as CFURL,
                                                        UTType.png.identifier as CFString, 1, nil) else {
    fatalError("Não foi possível criar o PNG")
}
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Falha ao gravar o PNG") }
print("Ícone gravado em \(output)")
