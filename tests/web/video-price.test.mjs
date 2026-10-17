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
    model: "seedance-2.5", mode: "text", variant: "default", duration: 4, resolution: "720p", aspect_ratio: "16:9",
  }, cap: 2 });
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
