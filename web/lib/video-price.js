// Only model/price controls leave the browser. Prompts, seeds, images and keys never enter estimates.
import { createPriceUpdater } from "./price.js";

function widget(node, name) {
  return node.widgets?.find((item) => item.name === name || item.name === `model.${name}`);
}

export function estimateFor(node, choices) {
  const selected = choices[widget(node, "model")?.value];
  if (!selected) return null;
  const body = { ...selected };
  for (const name of ["duration", "resolution", "aspect_ratio", "generate_audio"]) {
    const value = widget(node, name)?.value;
    if (value !== undefined) body[name] = value;
  }
  if (typeof body.duration !== "number" || !body.resolution || !body.aspect_ratio) return null;
  return { body, cap: widget(node, "max_usd")?.value };
}

export function mountVideoPrice(node, { client, t, choices, repeat = setInterval, stop = clearInterval,
  now = Date.now, updaterFactory = createPriceUpdater, keyEvents = globalThis }) {
  const label = node.addWidget("text", t("priceLabel"), t("priceNeedsKey"), () => {}, { serialize: false });
  label.disabled = true;
  let signature;
  let refreshed = 0;
  let disposed = false;
  const updater = updaterFactory({ client, t, onText(value) {
    label.value = value.text;
    node.setDirtyCanvas?.(true, true);
  } });
  const refresh = () => {
    if (disposed) return;
    const request = estimateFor(node, choices);
    if (!request) return;
    const next = JSON.stringify(request);
    // DynamicCombo replaces its widgets after a selection change. Read the current controls each
    // time, and periodically refresh unchanged estimates (including after a key was added).
    if (next !== signature || now() - refreshed >= 60_000) {
      signature = next;
      refreshed = now();
      updater.request("video", request.body, request.cap);
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
