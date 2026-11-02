// Local-only integration check for the same certificate-pinned client used by iOS.
import Foundation

@main struct ClientCheck {
    static func main() async throws {
        guard CommandLine.arguments.count == 4, let port = Int(CommandLine.arguments[1]) else { fatalError("test arguments missing") }
        let fingerprint = CommandLine.arguments[2]
        let secret = try String(contentsOfFile: CommandLine.arguments[3], encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        let pairing = Pairing(address: "127.0.0.1", port: port, fingerprint: fingerprint, secret: secret)
        let deviceID = UUID().uuidString
        let nonce = UUID().uuidString
        let data = try await SecureSync.post(pairing: pairing, endpoint: "recovery", body: ["nonce": nonce], testDeviceID: deviceID)
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any], json["nonce"] as? String == nonce else { fatalError("nonce mismatch") }
        let wrong = Pairing(address: pairing.address, port: port, fingerprint: String(repeating: "0", count: 64), secret: secret)
        do {
            _ = try await SecureSync.post(pairing: wrong, endpoint: "recovery", body: ["nonce": UUID().uuidString], testDeviceID: deviceID)
            fatalError("untrusted certificate accepted")
        } catch { print("trusted endpoint accepted; wrong fingerprint rejected") }
    }
}
