from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).parent / "dist" / "py-runtime"
root.mkdir(parents=True, exist_ok=True)
base = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/"

def fetch(name, checksum=None):
    target = root / name
    if checksum and target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == checksum:
        return
    temporary = target.with_suffix(target.suffix + ".partial")
    subprocess.run(["curl.exe", "-fLsS", "--retry", "2", "--max-time", "480",
                    base + name, "-o", str(temporary)], check=True)
    if checksum and hashlib.sha256(temporary.read_bytes()).hexdigest() != checksum:
        raise RuntimeError("Checksum mismatch: " + name)
    temporary.replace(target)

lock = root / "pyodide-lock.json"
if not lock.is_file():
    fetch(lock.name)
packages = json.loads(lock.read_text(encoding="utf-8"))["packages"]
needed = set()

def add(name):
    name = name.replace("_", "-")
    if name not in needed:
        needed.add(name)
        for dependency in packages[name]["depends"]:
            add(dependency)

for name in ("opencv-python", "scikit-image"):
    add(name)
runtime = ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"]
downloads = [(name, None) for name in runtime]
downloads.extend((packages[name]["file_name"], packages[name]["sha256"]) for name in sorted(needed))
with ThreadPoolExecutor(max_workers=20) as pool:
    list(pool.map(lambda item: fetch(*item), downloads))
print(sum(file.stat().st_size for file in root.iterdir()), "bytes", flush=True)
