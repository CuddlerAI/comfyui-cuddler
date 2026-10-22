// Only the speech estimate requires user text; it goes to the same Platform API through the local backend.
const value = (node, name) => node.widgets?.find(w => w.name === name || w.name === `model.${name}`)?.value;

export function imageEstimate(node, choices) {
  const selected = choices[value(node, "model")];
  if (!selected) return null;
  const resolution = value(node, "resolution"), aspect_ratio = value(node, "aspect_ratio");
  if (!resolution || !aspect_ratio) return null;
  const count = node.inputs?.filter(input => /^model\.references\.image\d+$/.test(input.name) && input.link != null).length || 0;
  return { body: { model: selected.model, variant: selected.variant, resolution, aspect_ratio,
    reference_image_count: count }, cap: value(node, "max_usd") };
}

export function speechEstimate(node, choices) {
  const selected = choices[value(node, "model")];
  const text = value(node, "text"), voice_id = value(node, "voice_id");
  if (!selected) return null;
  const script = value(node, "dialogue_script");
  if (typeof script === "string" && script.trim()) {
    if (text?.trim() || value(node, "second_speaker") === true) return null;
    const turns = script.split(/\r?\n/).filter(line => line.trim());
    if (!turns.length || turns.length > 50) return null;
    const lines = [];
    for (const turn of turns) {
      const match = /^\s*(10|[1-9])\s*:\s*(\S.*)$/.exec(turn);
      if (!match) return null;
      const voice = value(node, match[1] === "1" ? "voice_id" : `voice_id_${match[1]}`);
      if (!voice) return null;
      lines.push({ voice_id: voice, text: match[2].trim() });
    }
    const content = lines.length === 1 ? lines[0] : { dialogue: lines };
    return { body: { model: selected.model, output_format: "mp3", ...content }, cap: value(node, "max_usd") };
  }
  if (!text?.trim() || !voice_id) return null;
  let content = { text, voice_id };
  if (value(node, "second_speaker") === true) {
    const second = { text: value(node, "text_2"), voice_id: value(node, "voice_id_2") };
    if (!second.text?.trim() || !second.voice_id) return null;
    content = { dialogue: [content, second] };
  }
  return { body: { model: selected.model, output_format: "mp3", ...content }, cap: value(node, "max_usd") };
}

export function musicEstimate(node, choices) {
  const selected = choices[value(node, "model")];
  // Music is billed per generation, independent of prompt length or content.
  return selected ? { body: { model: selected.model, prompt: "Instrumental music.", output_format: "mp3" },
    cap: value(node, "max_usd") } : null;
}
