// Calls the plugin's own routes on this ComfyUI server. `fetchApi` is ComfyUI's api.fetchApi,
// passed in so this module stays testable without ComfyUI.

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

export function createClient(fetchApi, { onKeyChanged = () => {} } = {}) {
  const call = async (path, init) => {
    const response = await fetchApi(path, init);
    const body = await readJson(response);
    if (!response.ok) {
      const error = new Error(body.message || body.code || `HTTP ${response.status}`);
      error.code = body.code || "http_error";
      throw error;
    }
    return body;
  };
  const json = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const changeKey = async (init) => {
    const result = await call("/cuddler/key", init);
    onKeyChanged();
    return result;
  };
  return {
    status: ({ balance = false } = {}) => call(`/cuddler/status${balance ? "?balance=1" : ""}`),
    saveKey: (key) => changeKey(json("POST", { key })),
    clearKey: () => changeKey({ method: "DELETE" }),
    estimate: (kind, body) => call("/cuddler/estimate", json("POST", { kind, body })),
  };
}
