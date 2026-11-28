import Foundation
import Security

protocol OSSSecretStore: Sendable {
    func read(service: String, account: String) -> String?
    func write(_ value: String, service: String, account: String) -> Bool
    func delete(service: String, account: String) -> Bool
}

struct KeychainOSSSecretStore: OSSSecretStore {
    func read(service: String, account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else {
            return nil
        }
        return value
    }

    func write(_ value: String, service: String, account: String) -> Bool {
        let encoded = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: encoded,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            return true
        }
        guard updateStatus == errSecItemNotFound else {
            return false
        }
        var addQuery = query
        addQuery[kSecValueData as String] = encoded
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        return addStatus == errSecSuccess
    }

    func delete(service: String, account: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }
}

struct OSSConfig: @unchecked Sendable {
    static let baseURLDefaultsKey = "PrivateListen.ossBridgeBaseURL"
    static let legacyTokenDefaultsKey = "PrivateListen.ossBridgeToken"
    static let keychainService = "com.liuyuxin.privatelisten.oss-bridge"
    static let keychainAccount = "bridge-token"

    static let shared = OSSConfig()

    private let defaults: UserDefaults
    private let secretStore: any OSSSecretStore

    init(
        defaults: UserDefaults = .standard,
        secretStore: any OSSSecretStore = KeychainOSSSecretStore()
    ) {
        self.defaults = defaults
        self.secretStore = secretStore
    }

    var bridgeBaseURL: URL? {
        Self.normalizedBaseURL(from: defaults.string(forKey: Self.baseURLDefaultsKey))
    }

    var bridgeToken: String? {
        if let stored = secretStore.read(service: Self.keychainService, account: Self.keychainAccount)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !stored.isEmpty {
            return stored
        }
        guard let legacy = defaults.string(forKey: Self.legacyTokenDefaultsKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !legacy.isEmpty else {
            return nil
        }
        // Fail-closed: only remove the legacy plaintext entry and accept the
        // token once it is safely persisted in Keychain.
        guard secretStore.write(legacy, service: Self.keychainService, account: Self.keychainAccount) else {
            return nil
        }
        defaults.removeObject(forKey: Self.legacyTokenDefaultsKey)
        return legacy
    }

    var isConfigured: Bool {
        bridgeBaseURL != nil && bridgeToken != nil
    }

    func setBridgeBaseURL(_ raw: String?) {
        let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let trimmed, !trimmed.isEmpty else {
            defaults.removeObject(forKey: Self.baseURLDefaultsKey)
            return
        }
        if let normalized = Self.normalizedBaseURL(from: trimmed) {
            defaults.set(normalized.absoluteString, forKey: Self.baseURLDefaultsKey)
        } else {
            defaults.removeObject(forKey: Self.baseURLDefaultsKey)
        }
    }

    @discardableResult
    func setBridgeToken(_ token: String?) -> Bool {
        let trimmed = token?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let trimmed, !trimmed.isEmpty else {
            let deleted = secretStore.delete(service: Self.keychainService, account: Self.keychainAccount)
            defaults.removeObject(forKey: Self.legacyTokenDefaultsKey)
            return deleted
        }
        let wrote = secretStore.write(trimmed, service: Self.keychainService, account: Self.keychainAccount)
        if wrote {
            defaults.removeObject(forKey: Self.legacyTokenDefaultsKey)
        }
        return wrote
    }

    var fingerprint: String {
        let base = bridgeBaseURL?.absoluteString ?? ""
        let tokenDigest = bridgeToken.map { LibrarySnapshot.digest(Data($0.utf8)) } ?? ""
        return LibrarySnapshot.digest(Data("\(base)|\(tokenDigest)".utf8))
    }

    static func normalizedBaseURL(from raw: String?) -> URL? {
        guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty,
              var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              let host = components.host,
              !host.isEmpty,
              components.user == nil,
              components.password == nil else {
            return nil
        }
        components.fragment = nil
        components.query = nil
        while components.path.hasSuffix("/") {
            components.path.removeLast()
        }
        return components.url
    }
}
