// 本地日期锚点（dogfood R2-2：8-19 23:39 本地说"明天下午两点一面"被记成 8-21——
// 模型拿不到可靠的"今天"，只能依赖 LLM 服务端的隐式日期（时区口径不明），相对日期
// 推算漂移一天是业务事故）。asa-server 每轮把本机本地时间作为显式锚点注入用户消息，
// 模型的"今天/明天/下周"一律以该锚点推算，不再依赖任何隐式日期来源。
// 纯函数模块（不依赖 dsh 包），便于 node --test。

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** 进程本地时区（launchd 拉起时 TZ 缺省也能解析出系统时区）。 */
export function localTimeZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
}

const pad2 = (value) => String(value).padStart(2, "0");

/**
 * 生成日期锚点文本。默认取当前时刻 + 进程本地时区；测试可注入 Date 与 timeZone。
 * 形如：[当前本地时间：2026-08-19 23:39（星期三，Asia/Shanghai）…]
 */
export function localDateAnchorText(now = new Date(), timeZone = localTimeZone()) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    weekday: "short",
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map((part) => [part.type, part.value]));
  const weekday = WEEKDAYS[["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(parts.weekday)] || "";
  const stamp = `${parts.year}-${parts.month}-${parts.day} ${pad2(parts.hour)}:${pad2(parts.minute)}`;
  return `[当前本地时间：${stamp}（星期${weekday}，${timeZone}）。凡涉及"今天/明天/昨天/下周"等相对日期与面试、跟进等时间安排，一律以该本地日期为准推算，不得使用其他时区或猜测的日期]`;
}
