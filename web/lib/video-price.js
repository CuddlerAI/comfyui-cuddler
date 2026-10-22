// Video estimates contain only model/price controls. Other kinds supply their own request builder.
import { createPriceUpdater } from "./price.js";

function widget(node, name) {
  return node.widgets?.find((item) => item.name === name || item.name === `model.${name}`);
}

export function estimateFor(node, choices) {
  const selected = choices[widget(node, "model")?.value];
  if (!selected) return null;
  const body = { ...selected };
  for (const name of ["duration", "resolution", "aspect_ratio", "generate_audio", "fps"]) {
    const value = widget(node, name)?.value;
    if (value !== undefined) body[name] = value;
  }
  if (typeof body.duration !== "number" || !body.resolution || !body.aspect_ratio) return null;
  body.reference_image_count = node.inputs?.filter(input =>
    /^model\.(first_frame|last_frame|reference_images\.image\d+)$/.test(input.name) && input.link != null).length || 0;
  const startingPrice = node.inputs?.some(input => /^model\.reference_videos\.video\d+$/.test(input.name) && input.link != null) ||
    ["edit", "extend", "motion-control", "video-reference"].includes(body.mode);
  return { body, cap: widget(node, "max_usd")?.value, ...(startingPrice ? { startingPrice: true } : {}) };
}

export function mountVideoPrice(node, { client, t, choices, repeat = setInterval, stop = clearInterval,
  now = Date.now, updaterFactory = createPriceUpdater, keyEvents = globalThis,
  kind = "video", estimate = estimateFor }) {
  const label = node.addWidget("text", t("priceLabel"), t("priceNeedsKey"), () => {}, { serialize: false });
  label.disabled = true;
  // Recent ComfyUI frontends hide disabled widget values, but still draw the label.
  // Keep the quote read-only and visible without changing the stable widget name.
  label.label = `${t("priceLabel")}: ${label.value}`;
  let signature;
  let refreshed = 0;
  let disposed = false;
  let startingPrice = false;
  const updater = updaterFactory({ client, t, onText(value) {
    label.value = startingPrice && !value.error ? t("priceStarting", { price: value.text }) : value.text;
    label.label = `${t("priceLabel")}: ${label.value}`;
    label.color = value.over ? "#ef4444" : undefined;
    node.setDirtyCanvas?.(true, true);
  } });
  const refresh = () => {
    if (disposed) return;
    const request = estimate(node, choices);
    if (!request) return;
    const next = JSON.stringify(request);
    // DynamicCombo replaces its widgets after a selection change. Read the current controls each
    // time, and periodically refresh unchanged estimates (including after a key was added).
    if (next !== signature || now() - refreshed >= 60_000) {
      signature = next;
      refreshed = now();
      startingPrice = Boolean(request.startingPrice);
      updater.request(kind, request.body, request.cap);
    }
  };
  const timer = repeat(refresh, 500);
  const changed = () => { signature = undefined; refresh(); };
  keyEvents.addEventListener?.("cuddler-key-changed", changed);
  refresh();
  return () => {
    disposed = true;
    stop(timer);
    keyEvents.removeEventListener?.("cuddler-key-changed", changed);
    updater.dispose();
  };
}
