// Interface strings for the plugin's own pages, in English and Chinese.
// Pure module: ComfyUI loads every .js file under web/, so nothing here runs on import.

const STRINGS = {
  en: {
    keyNone: "No API key yet.",
    keyFile: "Using the key ending in {last4} ({mode}).",
    keyEnv: "Using the key from CUDDLER_API_KEY, ending in {last4} ({mode}).",
    keyError: "The saved key cannot be used: {message}",
    keyEnvError: "The key in CUDDLER_API_KEY cannot be used: {message}",
    modeLive: "live",
    modeTest: "test key, prices only",
    placeholder: "cud_sk_live_…",
    save: "Save",
    remove: "Remove",
    working: "Working…",
    saved: "Saved.",
    removed: "Removed.",
    balance: "Balance: {amount} available.",
    balanceError: "Balance unavailable: {message}",
    createKey: "Create a key",
    addFunds: "Add funds",
    envLocked: "Set by the environment variable; change it there.",
    priceNeedsKey: "Add an API key to see the price",
    priceLabel: "Estimated price",
    price: "≈ {amount}",
    // {amount} is the most a run can be charged (hold_usd), not the estimate.
    overCap: "Up to {amount}, above the {cap} limit",
    priceError: "Price unavailable: {message}",
  },
  zh: {
    keyNone: "还没有填写 API key。",
    keyFile: "正在使用末 4 位为 {last4} 的 key（{mode}）。",
    keyEnv: "正在使用环境变量 CUDDLER_API_KEY 里的 key，末 4 位为 {last4}（{mode}）。",
    keyError: "已保存的 key 不能用：{message}",
    keyEnvError: "环境变量 CUDDLER_API_KEY 里的 key 不能用：{message}",
    modeLive: "正式",
    modeTest: "测试 key，只能查价格",
    placeholder: "cud_sk_live_…",
    save: "保存",
    remove: "删除",
    working: "处理中…",
    saved: "已保存。",
    removed: "已删除。",
    balance: "余额：可用 {amount}。",
    balanceError: "暂时查不到余额：{message}",
    createKey: "创建 key",
    addFunds: "充值",
    envLocked: "key 由环境变量提供，请在环境变量里修改。",
    priceNeedsKey: "填写 API key 后显示价格",
    priceLabel: "预计价格",
    price: "约 {amount}",
    overCap: "最多 {amount}，超过上限 {cap}",
    priceError: "暂时无法估价：{message}",
  },
};

export function pickLanguage(locale) {
  return typeof locale === "string" && locale.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function createStrings(locale) {
  const table = STRINGS[pickLanguage(locale)];
  return (key, params = {}) =>
    (table[key] ?? STRINGS.en[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
}

export const LANGUAGES = Object.keys(STRINGS);
export const KEYS = Object.keys(STRINGS.en);
export const TABLES = STRINGS;
