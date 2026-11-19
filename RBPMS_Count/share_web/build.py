"""Build an allowlisted static bundle for the hosted browser edition."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
source = root / "share_web"
dist = source / "dist"
dist.mkdir(exist_ok=True)
for name in ("rbpms_web.py", "rbpms_counter_v2.py", "rbpms_counter_v3.py"):
    shutil.copyfile(root / name, dist / name)
model = (root / "rbpms_review_gui.py").read_text(encoding="utf-8").split("\nclass App:", 1)[0]
model = "\n".join(line for line in model.splitlines()
                  if not line.startswith(("import tkinter", "from tkinter", "from PIL")))
(dist / "rbpms_review_gui.py").write_text(model, encoding="utf-8")
for name in ("browser_api.py", "review-worker.js", "local-request.js"):
    shutil.copyfile(source / name, dist / name)
shutil.copyfile(root / "web_review" / "style.css", dist / "style.css")
html = (root / "web_review" / "index.html").read_text(encoding="utf-8")
html = html.replace("正在加载项目…", "正在加载浏览器计数引擎…")
(dist / "index.html").write_text(html, encoding="utf-8")
js = (root / "web_review" / "app.js").read_text(encoding="utf-8-sig")
js = 'import {localRequest} from "/local-request.js";\n' + js.replace("await fetch(", "await localRequest(")
js = js.replace('original.src = "/api/images/" + id + "/source.png";\n    await original.decode();',
    'const response = await localRequest("/api/images/" + id + "/source.png");\n'
    '    if (!response.ok) throw new Error("无法打开原图");\n'
    '    const url = URL.createObjectURL(await response.blob());\n'
    '    try { original.src = url; await original.decode(); } finally { URL.revokeObjectURL(url); }')
js += """
document.addEventListener("click", event => {
    const link = event.target.closest('a[href^="/api/"]');
    if (!link) return;
    event.preventDefault();
    run(async () => {
        const response = await localRequest(link.href);
        if (!response.ok) throw new Error((await response.json()).error);
        const download = document.createElement("a");
        download.href = URL.createObjectURL(await response.blob());
        download.download = new URL(link.href).pathname.split("/").pop();
        download.click();
        setTimeout(() => URL.revokeObjectURL(download.href), 30000);
    });
});
"""
(dist / "app.js").write_text(js, encoding="utf-8")
