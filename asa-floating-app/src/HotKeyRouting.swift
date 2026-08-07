import Carbon.HIToolbox

enum ASAHotKeyID: UInt32, CaseIterable, Equatable {
    case primaryOptionSpace = 1
    case backupCommandShiftA = 2
    case backupControlOptionA = 3
}

struct ASAHotKeySpec: Equatable {
    let id: ASAHotKeyID
    let keyCode: UInt32
    let modifiers: UInt32
    let label: String
}

enum ASAHotKeyDestination: Equatable {
    case agentMainWindow
    case compatibilityCopilot
}

// Pure, testable source of truth for global shortcut routing: which combos are
// registered, in what order, and where each one lands. AppDelegate only maps
// these specs to Carbon registration calls and keeps the trigger handling here
// so the routing decision stays deterministic.
enum ASAHotKeyRouting {
    static let globalHotKeySignature: OSType = 0x41534131 // "ASA1"

    static let defaultHotKeys: [ASAHotKeySpec] = [
        ASAHotKeySpec(
            id: .primaryOptionSpace,
            keyCode: UInt32(kVK_Space),
            modifiers: UInt32(optionKey),
            label: "Option+Space"
        ),
        ASAHotKeySpec(
            id: .backupCommandShiftA,
            keyCode: UInt32(kVK_ANSI_A),
            modifiers: UInt32(cmdKey | shiftKey),
            label: "Command+Shift+A"
        ),
        ASAHotKeySpec(
            id: .backupControlOptionA,
            keyCode: UInt32(kVK_ANSI_A),
            modifiers: UInt32(controlKey | optionKey),
            label: "Control+Option+A"
        ),
    ]

    static func destination(compatibilityCopilotEnabled: Bool) -> ASAHotKeyDestination {
        compatibilityCopilotEnabled ? .compatibilityCopilot : .agentMainWindow
    }
}
