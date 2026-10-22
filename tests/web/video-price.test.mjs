import assert from "node:assert/strict";
import test from "node:test";
import { estimateFor, mountVideoPrice } from "../../web/lib/video-price.js";

const choices = { selection: { model: "seedance-2.5", mode: "text", variant: "default" } };
function node() {
  return { widgets: Object.entries({ model: "selection", "model.duration": 4, "model.resolution": "720p",
    "model.aspect_ratio": "16:9", max_usd: 2, prompt: "private prompt", seed: 123 }).map(([name, value]) => ({ name, value })),
  addWidget(type, name, value) { const item = { type, name, value }; this.widgets.push(item); return item; } };
}

test("estimates contain only selected model and price parameters", () => {
  assert.deepEqual(estimateFor(node(), choices), { body: {
    model: "seedance-2.5", mode: "text", variant: "default", duration: 4, resolution: "720p", aspect_ratio: "16:9", reference_image_count: 0,
  }, cap: 2 });
});

test("video quotes count image links and label video-dependent prices as starting prices", () => {
  const n = node();
  n.inputs = [{ name: "model.reference_images.image0", link: 1 }, { name: "model.reference_images.image1", link: null },
    { name: "model.reference_videos.video0", link: 2 }];
  n.widgets.push({ name: "model.fps", value: 25 }, { name: "model.camera_fixed", value: true });
  const quote = estimateFor(n, { selection: { model: "seedance-2.5", mode: "reference", variant: "default" } });
  assert.equal(quote.body.reference_image_count, 1);
  assert.equal(quote.body.fps, 25);
  assert.equal(quote.startingPrice, true);
  assert.equal(quote.body.camera_fixed, undefined);
  assert.equal(quote.body.input_video_seconds, undefined);
});

test("silent model estimates override the API audio default without a toggle", () => {
  const silent = { selection: { model: "minimax-hailuo-2.3", mode: "text", variant: "standard",
    generate_audio: false } };
  assert.equal(estimateFor(node(), silent).body.generate_audio, false);
});

test("dynamic controls, periodic refresh and removal use one disposable updater", () => {
  const n = node(), requests = [];
  let tick, disposed = 0, stopped, time = 0;
  const remove = mountVideoPrice(n, { choices, client: {}, t: (key) => key, now: () => time,
    repeat(fn) { tick = fn; return 5; }, stop(id) { stopped = id; },
    updaterFactory({ onText }) { return { request(...args) { requests.push(args); onText({ text: "$0.66" }); },
      dispose() { disposed++; } }; } });
  assert.equal(requests.length, 1);
  assert.equal(n.widgets.at(-1).value, "$0.66");
  assert.equal(n.widgets.at(-1).disabled, true);
  assert.equal(n.widgets.at(-1).label, "priceLabel: $0.66");
  assert.equal(n.widgets.at(-1).name, "priceLabel");
  tick();
  assert.equal(requests.length, 1);
  n.widgets = n.widgets.map((widget) => widget.name === "model.duration" ? { ...widget, value: 5 } : widget);
  tick();
  assert.equal(requests[1][1].duration, 5);
  time = 60_000;
  tick();
  assert.equal(requests.length, 3);
  remove();
  tick();
  assert.equal(requests.length, 3);
  assert.equal(stopped, 5);
  assert.equal(disposed, 1);
});
