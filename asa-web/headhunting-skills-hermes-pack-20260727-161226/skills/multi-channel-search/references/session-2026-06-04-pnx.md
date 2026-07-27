# 鹏新旭 2026-06-04 搜索实录

## 猎聘搜索
- 变体: "鹏新旭 IE"(0人) / "鹏新旭(PST) IE"(3人) / "深圳市鹏新旭 IE工程师"(16人)
- 去重: 14人 → 人才库 46人(PQE 14 + IE 19 + AMHS 13)
- 方法: CDP 9223, chrome_profile_xhs, window.open 拦截 res_id

## X-SaaS 搜索
- 方法: `location.hash="#/app/candidate/list?SearchKeyWords=<urlencode>"`
- 全称变体: 226条记录, 提取 28 行(去匿名/误匹配后 19 人)
- 三个变体返回相同结果集
- 关键岗位: 陈志高(AMHS软件专家), 杨步政(Etch设备专家), 夏俭波(Litho设备工程师), 吴志东(量测技术主任), 陈书明(量测设备专家)
- 人才池: 芯片库(多数) + 大工业库(少数)

## 合并结果
- 去重: 54 人(猎聘 35 + X-SaaS 19, 0 重叠)
- 两渠道互补性极强

## Chrome CDP 稳定性
- 旧方案(独立profile + 清理SingletonLock) → 频繁崩溃, macOS杀后台
- 新方案: LaunchAgent `~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist`
  - KeepAlive: Crashed + NetworkState
  - ProcessType: Interactive, Nice: -10
  - 端口: 9223, Profile: chrome_profile_xhs
