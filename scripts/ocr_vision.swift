// 本地 macOS Vision OCR —— 逐字读出图片里的文字（中英混排）。
//
// 为什么需要它：内置多模态读图对**密集中文**经常读不准或读不出；
// 本地 Vision 稳定，且不依赖会话模型是否支持视觉。
//
// 为什么要串行：Vision OCR 并发执行会**静默失败**（返回空结果但不报错）。
// 本程序按参数顺序逐张处理，天然串行 —— 一次传多张即可，不要拆成并发任务。
//
// 用法：
//   swiftc -O scripts/ocr_vision.swift -o /tmp/ocr_vision
//   /tmp/ocr_vision frame_001.jpg frame_002.jpg
//
// 输出：每张图一个 === 文件名 === 段，按视觉行序逐行输出文字。

import Foundation
import CoreImage
import Vision

let args = Array(CommandLine.arguments.dropFirst())
if args.isEmpty {
    FileHandle.standardError.write("用法: ocr_vision <image_path> [...]\n".data(using: .utf8)!)
    exit(2)
}

for path in args {
    let url = URL(fileURLWithPath: path)
    guard let data = try? Data(contentsOf: url),
          let ciImage = CIImage(data: data) else {
        print("=== \(url.lastPathComponent) ===")
        print("[无法读取图片]")
        continue
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(ciImage: ciImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        print("=== \(url.lastPathComponent) ===")
        print("[识别失败: \(error)]")
        continue
    }

    let results = request.results ?? []
    print("=== \(url.lastPathComponent) ===")
    if results.isEmpty {
        print("[未识别到文字]")
    } else {
        // 按 boundingBox 的 y 坐标从上到下排序（Vision 坐标原点在左下）
        let sorted = results.sorted { a, b in
            let ya = a.boundingBox.midY
            let yb = b.boundingBox.midY
            if abs(ya - yb) > 0.012 { return ya > yb }
            return a.boundingBox.minX < b.boundingBox.minX
        }
        for obs in sorted {
            if let top = obs.topCandidates(1).first {
                print(top.string)
            }
        }
    }
}
