import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createClient } from "./lib/client.js";
import { createStrings } from "./lib/i18n.js";
import { mountVideoPrice } from "./lib/video-price.js";

const client = createClient((path, init) => api.fetchApi(path, init));

app.registerExtension({
  name: "Cuddler.Video",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "CuddlerVideo") return;
    const choices = nodeData.input?.required?.model?.[1]?.cuddler_choices ?? {};
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      const result = created?.apply(this, args);
      const seed = this.widgets?.find((widget) => widget.name === "seed");
      if (seed) seed.value = crypto.getRandomValues(new Uint32Array(1))[0] & 0x7fffffff;
      const locale = app.extensionManager?.setting?.get?.("Comfy.Locale") || navigator.language;
      const dispose = mountVideoPrice(this, { client, t: createStrings(locale), choices });
      const removed = this.onRemoved;
      this.onRemoved = function (...values) {
        dispose();
        return removed?.apply(this, values);
      };
      return result;
    };
  },
});
