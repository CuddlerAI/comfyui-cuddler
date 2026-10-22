// Entry point ComfyUI loads for the Cuddler plugin: registers the "Cuddler" settings page.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createClient } from "./lib/client.js";
import { createStrings } from "./lib/i18n.js";
import { renderSettingsPanel } from "./lib/settings-panel.js";

const client = createClient((path, init) => api.fetchApi(path, init), {
  onKeyChanged: () => window.dispatchEvent(new Event("cuddler-key-changed")),
});
const locale = () => app.extensionManager?.setting?.get?.("Comfy.Locale") || navigator.language;

app.registerExtension({
  name: "Cuddler.Platform",
  settings: [
    {
      id: "Cuddler.Account",
      category: ["Cuddler", "Account", "Key"],
      name: "Cuddler API key",
      defaultValue: "",
      // A custom form: ComfyUI calls this to render it, and the key never passes through the setter.
      type: () => renderSettingsPanel({ client, t: createStrings(locale()) }),
    },
  ],
});
