"""Render an .excalidraw scene to SVG/PNG with Excalidraw's own exporter.

    python3 tools/excalidraw_render.py docs/architecture.excalidraw out.png

Loads @excalidraw/excalidraw in headless Chrome, calls its exportToSvg on the
scene, and rasterises the result with rsvg-convert. Same rendering code as the
Excalidraw app; needs network access to the CDN and Google Chrome.
"""
import json, pathlib, re, subprocess, sys, tempfile, html

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
scene = json.loads(pathlib.Path(sys.argv[1]).read_text())
out_png = pathlib.Path(sys.argv[2])
page = """<!doctype html><meta charset=utf-8><body><pre id=out>PENDING</pre>
<script type=module>
import { exportToSvg } from "https://esm.sh/@excalidraw/excalidraw@0.18.0?bundle";
const scene = %s;
try {
  const svg = await exportToSvg({ elements: scene.elements.filter(e => !e.isDeleted),
    appState: { exportBackground: true, viewBackgroundColor: "#ffffff", exportWithDarkMode: false },
    files: scene.files || {} });
  document.getElementById("out").textContent = "SVG" + "BEGIN" + svg.outerHTML + "SVG" + "DONE";
} catch (err) { document.getElementById("out").textContent = "RENDER" + "FAIL " + (err && err.stack || err); }
</script>""" % json.dumps(scene)
with tempfile.TemporaryDirectory() as tmp:
    f = pathlib.Path(tmp) / "render.html"
    f.write_text(page)
    dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=90000",
                          "--run-all-compositor-stages-before-draw", "--dump-dom", f.as_uri()],
                         capture_output=True, text=True, timeout=120).stdout
m = re.search(r"SVGBEGIN(.*)SVGDONE", dom, re.S)   # markers never appear literally in the page source
if not m:
    fail = re.search(r"RENDERFAIL[^<]*", dom)
    sys.exit("render failed: " + (fail.group(0) if fail else dom[-600:]))
svg = html.unescape(m.group(1))
svg_path = out_png.with_suffix(".svg")
svg_path.write_text(svg)
subprocess.run(["rsvg-convert", "-w", sys.argv[3] if len(sys.argv) > 3 else "2400",
                str(svg_path), "-o", str(out_png)], check=True)
print(out_png)
