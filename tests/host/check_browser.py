"""Browser check: the Cuddler settings panel works inside the real ComfyUI page.

    python tests/host/check_browser.py --base http://127.0.0.1:8188 [--channel chrome]

Needs `pip install playwright` and, without --channel, `python -m playwright install chromium`.
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

KEY = "cud_sk_live_" + "B" * 24
PANEL = "#cuddler-check"
RENDER = """() => {
  const extension = window.app.extensions.find((item) => item.name === "Cuddler.Platform");
  const panel = extension.settings[0].type("Cuddler.Account", () => {}, "", {});
  panel.id = "cuddler-check";
  document.body.appendChild(panel);
}"""


def fill(page, selector, value):
    # Act on the panel directly: ComfyUI may show a welcome dialog that covers the page on first start.
    page.evaluate("([selector, value]) => { const input = document.querySelector(selector); input.value = value;"
                  " input.dispatchEvent(new Event('input', { bubbles: true })); }", [selector, value])


def click(page, selector):
    page.evaluate("(selector) => document.querySelector(selector).click()", selector)


def text_includes(selector, text):
    return f"() => (document.querySelector({json.dumps(selector)})?.textContent || '').includes({json.dumps(text)})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--channel", default=None, help="use an installed browser, e.g. chrome")
    args = parser.parse_args()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel) if args.channel else playwright.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.base, wait_until="domcontentloaded")
        page.wait_for_function("() => Array.isArray(window.app?.extensions) && "
                               "window.app.extensions.some((item) => item.name === 'Cuddler.Platform')",
                               timeout=120_000)
        page.evaluate(RENDER)
        page.wait_for_function(text_includes(f"{PANEL} [data-cuddler=status]", "key"), timeout=30_000)
        fill(page, f"{PANEL} [data-cuddler=key]", KEY)
        click(page, f"{PANEL} [data-cuddler=save]")
        page.wait_for_function(text_includes(f"{PANEL} [data-cuddler=status]", "BBBB"), timeout=30_000)
        page.wait_for_function(text_includes(f"{PANEL} [data-cuddler=balance]", "12.34"), timeout=30_000)
        for node_id in ("CuddlerVideo", "CuddlerImage", "CuddlerSpeech", "CuddlerMusic"):
            page.wait_for_function("(id) => window.LiteGraph?.registered_node_types?.[id]", arg=node_id, timeout=30_000)
            page.evaluate("""(id) => {
              const node = window.LiteGraph.createNode(id);
              window.app.graph.add(node);
              window.__cuddlerCheckNode = node;
            }""", node_id)
            if node_id == "CuddlerSpeech":
                page.wait_for_function("() => window.__cuddlerCheckNode.widgets.some(w => w.name === 'model.text' || w.name === 'text')")
                page.evaluate("() => { window.__cuddlerCheckNode.widgets.find(w => w.name === 'model.text' || w.name === 'text').value = 'Hello world.'; }")
            page.wait_for_function("() => window.__cuddlerCheckNode.widgets.some((widget) => "
                                   "typeof widget.value === 'string' && widget.value.includes('$0.66'))",
                                   timeout=30_000)
            page.wait_for_function("() => window.__cuddlerCheckNode.widgets.some((widget) => "
                                   "widget.disabled === true && typeof widget.label === 'string' && "
                                   "widget.label.includes('$0.66'))", timeout=30_000)
            workflow = page.evaluate("() => window.__cuddlerCheckNode.serialize()")
            if KEY in json.dumps(workflow):
                raise AssertionError("the API key reached a workflow")
            if node_id == "CuddlerVideo":
                page.evaluate("""() => {
                  const node = window.__cuddlerCheckNode;
                  const model = node.widgets.find(w => w.name === 'model');
                  const choice = model.options.values.find(v => v.includes('(seedance-2.5) / Reference to video /'));
                  if (!choice) throw new Error('Cuddler reference selector missing');
                  model.value = choice;
                  model.callback(choice);
                }""")
                page.wait_for_function("() => window.__cuddlerCheckNode.inputs.some(i => i.name === 'model.reference_images.image0')")
                page.evaluate("""() => {
                  const source = window.LiteGraph.createNode('CuddlerHostMediaFixture');
                  window.app.graph.add(source);
                  window.__cuddlerCheckSource = source;
                  const node = window.__cuddlerCheckNode;
                  source.connect(0, node, node.inputs.findIndex(i => i.name === 'model.reference_images.image0'));
                  source.connect(2, node, node.inputs.findIndex(i => i.name === 'model.reference_videos.video0'));
                }""")
                page.wait_for_function("() => window.__cuddlerCheckNode.inputs.some(i => i.name === 'model.reference_images.image1')")
                page.wait_for_function("() => window.__cuddlerCheckNode.widgets.some(w => typeof w.value === 'string' && w.value.includes('Starting price'))")
                workflow = page.evaluate("() => window.__cuddlerCheckNode.serialize()")
                if KEY in json.dumps(workflow):
                    raise AssertionError('the API key reached an advanced workflow')
                page.evaluate("() => window.app.graph.remove(window.__cuddlerCheckSource)")
            page.evaluate("() => window.app.graph.remove(window.__cuddlerCheckNode)")
        settings = page.evaluate("() => fetch('/api/settings').then((response) => response.json())")
        click(page, f"{PANEL} [data-cuddler=remove]")
        page.wait_for_function(f"() => !({text_includes(f'{PANEL} [data-cuddler=status]', 'BBBB')})()",
                               timeout=30_000)
        browser.close()
    leaked = KEY in json.dumps(settings)
    own_errors = [error for error in errors if "cuddler" in error.lower()]
    if leaked or own_errors:
        print(f"FAIL key in ComfyUI settings: {leaked}; page errors: {own_errors}")
        return 1
    print("PASS settings, four price labels, reference mode and Autogrow work; no key in settings or workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
