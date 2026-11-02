import SwiftUI
import AppKit
import CoreImage.CIFilterBuiltins
import Darwin

@MainActor final class Receiver: ObservableObject {
    @Published var status = "接收端未启动"
    @Published var pairingImage: NSImage?
    @Published var address = ""
    private var process: Process?
    private var pipe: Pipe?
    private var incoming = Data()
    private let root = Bundle.main.object(forInfoDictionaryKey: "FitnessVaultRoot") as? String ?? ""
    private var identity: URL { URL(fileURLWithPath: root).appendingPathComponent("90_System/local/fitness/identity") }
    private var database: URL { URL(fileURLWithPath: root).appendingPathComponent("90_System/local/fitness/fitness.sqlite3") }

    init() { address = localAddress() ?? "" }
    func start(rotate: Bool) {
        stop()
        guard !root.isEmpty, let script = Bundle.main.url(forResource: "fitness_service", withExtension: "py") else {
            status = "缺少接收端文件，无法启动。"; return
        }
        let task = Process()
        let output = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        var args = [script.path, "--host", "0.0.0.0", "--port", "42591", "--directory", identity.path, "--db", database.path]
        if rotate { args.append("--rotate-pairing") }
        task.arguments = args
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        task.terminationHandler = { [weak self] task in
            DispatchQueue.main.async {
                guard let self, self.process === task else { return }
                self.process = nil
                self.pairingImage = nil
                self.status = "接收端已停止。"
            }
        }
        do {
            try task.run()
            process = task
            pipe = output
            incoming = Data()
            status = "正在启动接收端…"
            output.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                DispatchQueue.main.async {
                    guard let self, self.process === task else { return }
                    self.incoming.append(data)
                    guard let newline = self.incoming.firstIndex(of: 10) else { return }
                    let line = self.incoming.prefix(upTo: newline)
                    handle.readabilityHandler = nil
                    guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                          let port = object["port"] as? Int, let fingerprint = object["certificate_sha256"] as? String else {
                        self.status = "接收端启动信息无效。"; self.stop(); return
                    }
                    self.status = "接收端运行中，手机与 Mac 请连接同一网络。"
                    guard rotate else { return }
                    do {
                        let secret = try String(contentsOf: self.identity.appendingPathComponent("pairing-secret"), encoding: .utf8)
                        guard !self.address.isEmpty else { self.status = "找不到 Mac 本地地址；请填写地址后重新配对。"; return }
                        let pairing: [String: Any] = ["address": self.address, "port": port, "fingerprint": fingerprint, "secret": secret]
                        let encoded = try JSONSerialization.data(withJSONObject: pairing).base64EncodedString()
                        let filter = CIFilter.qrCodeGenerator()
                        filter.message = Data(("fitness-pair-v1:" + encoded).utf8)
                        guard let image = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else { return }
                        let representation = NSCIImageRep(ciImage: image)
                        let result = NSImage(size: representation.size)
                        result.addRepresentation(representation)
                        self.pairingImage = result
                    } catch { self.status = "无法生成配对码。" }
                }
            }
        } catch { status = "接收端启动失败：\(error.localizedDescription)" }
    }
    func stop() {
        pipe?.fileHandleForReading.readabilityHandler = nil
        if let process, process.isRunning { process.terminate(); process.waitUntilExit() }
        process = nil
        pipe = nil
        pairingImage = nil
        status = "接收端已停止。"
    }
}

func localAddress() -> String? {
    var interfaces: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&interfaces) == 0 else { return nil }
    defer { freeifaddrs(interfaces) }
    var cursor = interfaces
    while let current = cursor {
        defer { cursor = current.pointee.ifa_next }
        let item = current.pointee
        guard let address = item.ifa_addr, address.pointee.sa_family == UInt8(AF_INET),
              String(cString: item.ifa_name).hasPrefix("en"), (item.ifa_flags & UInt32(IFF_UP)) != 0 else { continue }
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        if getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
            return String(cString: host)
        }
    }
    return nil
}

@main struct FitnessMacApp: App {
    @StateObject private var receiver = Receiver()
    var body: some Scene {
        WindowGroup("健身记录同步") {
            VStack(alignment: .leading, spacing: 16) {
                Text("健身记录同步").font(.largeTitle.bold())
                Text(receiver.status).foregroundStyle(.secondary)
                TextField("Mac 的局域网地址", text: $receiver.address).textFieldStyle(.roundedBorder)
                HStack {
                    Button("开始接收") { receiver.start(rotate: false) }
                    Button("新设备配对") { receiver.start(rotate: true) }
                    Button("停止") { receiver.stop() }
                }
                if let image = receiver.pairingImage {
                    Image(nsImage: image).interpolation(.none).resizable().scaledToFit().frame(width: 240, height: 240)
                    Text("用 iPhone 健身 App 扫描。配对码可访问你的训练记录，请勿分享。新设备配对会撤销旧手机的访问。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(24).frame(minWidth: 440, minHeight: 440).onDisappear { receiver.stop() }
        }.windowResizability(.contentMinSize)
    }
}
