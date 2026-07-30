import Foundation

struct NativeContextPrivacy {
    static func clipboardMetadata(hasText: Bool, changeCount: Int) -> [String: Any] {
        [
            "has_text": hasText,
            "change_count": changeCount,
        ]
    }
}
