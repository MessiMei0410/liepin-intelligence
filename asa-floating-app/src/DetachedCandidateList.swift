import AppKit

/// 独立候选名单窗口的数据源：原生 NSTableView 渲染，点选行回调打开人选详情。
final class DetachedCandidateDataSource: NSObject, NSTableViewDataSource {
    private let candidates: [[String: Any]]
    init(candidates: [[String: Any]]) {
        self.candidates = candidates
    }
    func numberOfRows(in tableView: NSTableView) -> Int {
        candidates.count
    }
    func tableView(_ tableView: NSTableView, objectValueFor tableColumn: NSTableColumn?, row: Int) -> Any? {
        guard row >= 0 && row < candidates.count else { return nil }
        let item = candidates[row]
        switch tableColumn?.identifier.rawValue {
        case "name":
            return item["name"] as? String ?? "未知"
        case "detail":
            let company = item["company"] as? String ?? ""
            let title = item["title"] as? String ?? ""
            let stage = item["stage"] as? String ?? ""
            let parts = [company, title].filter { !$0.isEmpty }
            return parts.joined(separator: " · ") + (stage.isEmpty ? "" : "（\(stage)）")
        default:
            return nil
        }
    }
}

/// 独立候选名单窗口的 delegate：绘制行样式 + 点击回调（传出整行数据）。
final class DetachedCandidateDelegate: NSObject, NSTableViewDelegate {
    private let candidates: [[String: Any]]
    private let onSelect: ([String: Any]) -> Void
    init(candidates: [[String: Any]], onSelect: @escaping ([String: Any]) -> Void) {
        self.candidates = candidates
        self.onSelect = onSelect
    }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0 && row < candidates.count else { return nil }
        let identifier = NSUserInterfaceItemIdentifier("cell-\(tableColumn?.identifier.rawValue ?? "x")")
        let cell: NSTableCellView
        if let reused = tableView.makeView(withIdentifier: identifier, owner: nil) as? NSTableCellView {
            cell = reused
        } else {
            cell = NSTableCellView()
            cell.identifier = identifier
            let label = NSTextField(labelWithString: "")
            label.translatesAutoresizingMaskIntoConstraints = false
            label.lineBreakMode = .byTruncatingTail
            label.font = tableColumn?.identifier.rawValue == "name"
                ? NSFont.systemFont(ofSize: 13, weight: .semibold)
                : NSFont.systemFont(ofSize: 11)
            label.textColor = tableColumn?.identifier.rawValue == "name"
                ? .labelColor : .secondaryLabelColor
            cell.addSubview(label)
            cell.textField = label
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
                label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
        }
        let item = candidates[row]
        switch tableColumn?.identifier.rawValue {
        case "name":
            cell.textField?.stringValue = item["name"] as? String ?? "未知"
        case "detail":
            let company = item["company"] as? String ?? ""
            let title = item["title"] as? String ?? ""
            let stage = item["stage"] as? String ?? ""
            let parts = [company, title].filter { !$0.isEmpty }
            cell.textField?.stringValue = parts.joined(separator: " · ") + (stage.isEmpty ? "" : "（\(stage)）")
        default:
            cell.textField?.stringValue = ""
        }
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let tableView = notification.object as? NSTableView else { return }
        let row = tableView.selectedRow
        guard row >= 0 && row < candidates.count else { return }
        onSelect(candidates[row])
    }
}
