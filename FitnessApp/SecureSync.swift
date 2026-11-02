import Foundation
import Security
import CryptoKit

struct Pairing: Codable {
    let address: String
    let port: Int
    let fingerprint: String
    let secret: String

    var url: URL? {
        // No public Internet destination, credentials, query or path accepted.
        let host = address.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        let octets = parts.compactMap { UInt8($0) }
        let localIPv4 = parts.count == 4 && octets.count == 4 && (octets[0] == 10 || octets[0] == 127 ||
            (octets[0] == 192 && octets[1] == 168) ||
            (octets[0] == 172 && (16...31).contains(octets[1])) ||
            (octets[0] == 169 && octets[1] == 254))
        guard localIPv4 || (host.hasSuffix(".local") && host.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "." || $0 == "-") })) || host == "localhost",
              (1...65535).contains(port), fingerprint.count == 64,
              fingerprint.allSatisfy({ $0.isHexDigit && $0.isASCII }), secret.count >= 32 else { return nil }
        var components = URLComponents()
        components.scheme = "https"
        components.host = host
        components.port = port
        return components.url
    }
}

enum PairingStorage {
    static var service: String {
        ProcessInfo.processInfo.arguments.contains("--ui-testing") ? "local.justinyang.fitness.pairing.ui-test" : "local.justinyang.fitness.pairing"
    }
    static func resetTestIdentity() {
        guard ProcessInfo.processInfo.arguments.contains("--ui-testing") else { return }
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }
    static func read() -> Pairing? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: "mac", kSecReturnData as String: true]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(Pairing.self, from: data)
    }
    static func save(_ pairing: Pairing) throws {
        guard pairing.url != nil else { throw SyncError.badPairing }
        let data = try JSONEncoder().encode(pairing)
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: "mac"]
        let update = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecItemNotFound {
            var attributes = query
            attributes[kSecValueData as String] = data
            attributes[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            let status = SecItemAdd(attributes as CFDictionary, nil)
            guard status == errSecSuccess else { throw SyncError.keychain(status) }
        } else if update != errSecSuccess { throw SyncError.keychain(update) }
    }
}

enum DeviceIdentity {
    static func get() throws -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: PairingStorage.service,
                                    kSecAttrAccount as String: "device", kSecReturnData as String: true]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecSuccess, let data = item as? Data, let value = String(data: data, encoding: .utf8), UUID(uuidString: value) != nil { return value }
        guard status == errSecItemNotFound else { throw SyncError.keychain(status) }
        let value = UUID().uuidString
        let attributes: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: PairingStorage.service, kSecAttrAccount as String: "device",
            kSecValueData as String: Data(value.utf8), kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let added = SecItemAdd(attributes as CFDictionary, nil)
        guard added == errSecSuccess else { throw SyncError.keychain(added) }
        return value
    }
}

enum SyncError: LocalizedError {
    case badPairing, keychain(OSStatus), response, nonce, rejected
    var errorDescription: String? {
        switch self {
        case .badPairing: return "配对信息无效；请检查本地地址、端口、证书指纹和密钥。"
        case .keychain(let status): return "无法访问设备钥匙串（状态 \(status)）。"
        case .response: return "Mac 返回的数据无效。"
        case .nonce: return "Mac 回执不属于当前请求，未应用。"
        case .rejected: return "Mac 拒绝了请求，请检查配对和接收端状态。"
        }
    }
}

final class PinnedSession: NSObject, URLSessionDelegate, URLSessionTaskDelegate {
    private let fingerprint: String
    init(fingerprint: String) { self.fingerprint = fingerprint.lowercased() }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let data = SecCertificateCopyData(certificate) as Data
        let actual = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard actual == fingerprint else { completionHandler(.cancelAuthenticationChallenge, nil); return }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

enum SecureSync {
    static func post(pairing: Pairing, endpoint: String, body: [String: Any], testDeviceID: String? = nil) async throws -> Data {
        guard let base = pairing.url, ["sync", "recovery"].contains(endpoint) else { throw SyncError.badPairing }
        var request = URLRequest(url: base.appendingPathComponent(endpoint), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("Bearer \(pairing.secret)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload = body
        payload["deviceId"] = try testDeviceID ?? DeviceIdentity.get()
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        let config = URLSessionConfiguration.ephemeral
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.httpCookieStorage = nil
        let delegate = PinnedSession(fingerprint: pairing.fingerprint)
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw SyncError.rejected }
        return data
    }
}
