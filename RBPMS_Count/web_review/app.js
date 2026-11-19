const byId = id => document.getElementById(id);
const canvas = byId("canvas");
const ctx = canvas.getContext("2d");
const colors = {dark_review:"#ff9e40", strong_candidate:"#00ffff", weak_review:"#ffeb28", rejected_review:"#ff46cd"};
const channels = {green:"绿色", red:"红色", auto:"自动判断"};
const filters = [...document.querySelectorAll(".filter")];
let state, photo, busy = false;
let scale = 1, offsetX = 0, offsetY = 0, drag, fitted = true;

async function api(url, options) {
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    return result;
}

function endpoint(suffix = "") {
    return "/api/images/" + state.id + suffix;
}

function status(message) {
    byId("status").textContent = message;
}

async function run(work) {
    if (busy) return;
    busy = true;
    controls();
    try {
        await work();
    } catch (error) {
        status(error.message);
    } finally {
        busy = false;
        controls();
    }
}

function controls() {
    byId("undo").disabled = busy || !state?.canUndo;
    byId("redo").disabled = busy || !state?.canRedo;
    byId("importButton").disabled = busy;
    byId("showBatch").disabled = busy;
    byId("manageImages").disabled = busy;
    const completed = state && state.reviewed_count !== null && !state.review_reopened;
    byId("completeReview").disabled = busy || !state || completed;
    byId("reopenReview").hidden = !completed;
    byId("reopenReview").disabled = busy;
    byId("detectDark").disabled = busy || !state || state.cells.some(cell => cell.category === "dark_review");
    const included = new Set(state?.included);
    for (const [id, category] of [["includeDark", "dark_review"], ["includeWeak", "weak_review"], ["includeArtifacts", "rejected_review"]]) {
        byId(id).disabled = busy || !state?.cells.some(cell => cell.category === category && !included.has(cell.id));
    }
    byId("exports").hidden = !state || busy;
    document.querySelectorAll("nav button").forEach(button => { button.disabled = busy; });
}

async function library() {
    const images = await api("/api/images");
    byId("images").replaceChildren(...images.map(image => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = image.name + " · " + image.final_count;
        button.title = image.image_path + " · " + image.source;
        button.dataset.imageId = image.id;
        button.setAttribute("aria-current", String(image.id === state?.id));
        button.onclick = () => run(() => openImage(image.id));
        return button;
    }));
    controls();
    return images;
}

async function openImage(id) {
    const next = await api("/api/images/" + id);
    const original = new Image();
    original.src = "/api/images/" + id + "/source.png";
    await original.decode();
    photo = original;
    state = next;
    for (const button of byId("images").children) {
        button.setAttribute("aria-current", String(button.dataset.imageId === id));
    }
    localStorage.setItem("rbpms-last-image", id);
    byId("empty").hidden = true;
    byId("csv").href = endpoint("/count.csv");
    byId("png").href = endpoint("/review.png");
    fit();
    update();
    status([state.name, state.source, channels[state.channel]].filter(Boolean).join(" · "));
}

function update() {
    const included = new Set(state.included);
    const manual = Object.keys(state.added).length;
    const reviewed = Object.keys(state.decisions).length;
    const total = included.size + manual;
    byId("total").textContent = total;
    byId("reviewStatus").textContent = state.reviewed_count === null ?
        "审核完成后，确认计数将写入导出的 CSV。" : "已完成审核 · " + state.reviewed_count + " 个细胞";
    if (state.review_reopened) {
        byId("reviewStatus").textContent = "审核进行中 · CSV 仍使用上次确认的 " + state.reviewed_count + " 个细胞";
    }
    const active = byId("images").querySelector('[aria-current="true"]');
    if (active) active.textContent = state.name + " · " + total;
    const values = [
        ["自动高可信", state.cells.filter(cell => cell.category === "strong_candidate").length],
        ["确认暗细胞", state.cells.filter(cell => cell.category === "dark_review" && included.has(cell.id)).length],
        ["确认弱阳性", state.cells.filter(cell => cell.category === "weak_review" && included.has(cell.id)).length],
        ["人工新增", manual],
        ["人工排除", Object.values(state.decisions).filter(value => !value).length],
        ["待审核候选", state.cells.length - reviewed],
        ["审核进度", reviewed + " / " + state.cells.length]
    ];
    byId("counts").replaceChildren(...values.flatMap(([label, value]) => {
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = label;
        dd.textContent = value;
        return [dt, dd];
    }));
    byId("progress").max = Math.max(1, state.cells.length);
    byId("progress").value = reviewed;
    controls();
    draw();
}

function draw() {
    const ratio = devicePixelRatio;
    const width = Math.round(canvas.clientWidth * ratio);
    const height = Math.round(canvas.clientHeight * ratio);
    if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    if (!state) return;
    byId("zoom").textContent = Math.round(scale * 100) + "%";
    ctx.translate(offsetX, offsetY);
    ctx.scale(scale, scale);
    ctx.imageSmoothingEnabled = scale < 1;
    ctx.drawImage(photo, 0, 0);
    if (!byId("overlay").checked) return;
    ctx.lineWidth = Number(byId("lineWidth").value) / scale;
    ctx.font = (12 / scale) + "px sans-serif";
    const visible = new Set(filters.filter(filter => filter.checked).map(filter => filter.value));
    const included = new Set(state.included);
    for (const cell of state.cells) {
        if (!visible.has(cell.category)) continue;
        ctx.strokeStyle = colors[cell.category];
        ctx.globalAlpha = state.decisions[cell.id] === false ? 0.38 : 0.92;
        for (const polygon of cell.polygons) {
            ctx.beginPath();
            polygon.forEach(([x, y], index) => {
                if (index === 0) ctx.moveTo(x + .5, y + .5);
                else ctx.lineTo(x + .5, y + .5);
            });
            ctx.closePath();
            ctx.stroke();
        }
        if (included.has(cell.id)) marker(cell.x, cell.y, 2, colors[cell.category], true);
        if (byId("ids").checked) ctx.fillText(cell.id, cell.x + 4 / scale, cell.y);
    }
    ctx.globalAlpha = 1;
    for (const [id, [x, y]] of Object.entries(state.added)) {
        marker(x, y, 5, "#ffffff", false);
        if (byId("ids").checked) ctx.fillText(id, x + 7 / scale, y);
    }
}

function marker(x, y, radius, color, fill) {
    ctx.beginPath();
    ctx.arc(x + .5, y + .5, radius / scale, 0, Math.PI * 2);
    ctx.strokeStyle = ctx.fillStyle = color;
    if (fill) ctx.fill();
    else ctx.stroke();
}

function center(nextScale) {
    if (!state) return;
    scale = nextScale;
    offsetX = (canvas.clientWidth - state.width * scale) / 2;
    offsetY = (canvas.clientHeight - state.height * scale) / 2;
    draw();
}

function fit() {
    fitted = true;
    if (state) center(Math.min(canvas.clientWidth / state.width, canvas.clientHeight / state.height));
}

function position(event) {
    const rect = canvas.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
}

canvas.addEventListener("wheel", event => {
    if (!state) return;
    event.preventDefault();
    fitted = false;
    const [x, y] = position(event);
    const next = Math.max(0.03, Math.min(20, scale * (event.deltaY < 0 ? 1.2 : 1 / 1.2)));
    offsetX = x - (x - offsetX) * next / scale;
    offsetY = y - (y - offsetY) * next / scale;
    scale = next;
    draw();
}, {passive:false});

canvas.onpointerdown = event => {
    if (!state || busy || drag || ![0, 1].includes(event.button)) return;
    canvas.setPointerCapture(event.pointerId);
    drag = {id:event.pointerId, origin:position(event), offsetX, offsetY, moved:false, button:event.button};
};
canvas.onpointermove = event => {
    if (drag?.id !== event.pointerId) return;
    const [x, y] = position(event);
    const dx = x - drag.origin[0], dy = y - drag.origin[1];
    if (Math.hypot(dx, dy) > 4) drag.moved = true;
    if (drag.moved) {
        fitted = false;
        offsetX = drag.offsetX + dx;
        offsetY = drag.offsetY + dy;
        draw();
    }
};
canvas.onpointerup = event => {
    if (drag?.id !== event.pointerId) return;
    const click = !drag.moved && drag.button === 0;
    drag = undefined;
    const mode = byId("mode").value;
    if (!click || mode === "pan") return;
    const [sx, sy] = position(event);
    const x = Math.floor((sx - offsetX) / scale), y = Math.floor((sy - offsetY) / scale);
    if (x >= 0 && x < state.width && y >= 0 && y < state.height) change("edit", {x,y,mode});
};
canvas.onlostpointercapture = () => { drag = undefined; };

function change(action, detail = {}) {
    if (!state) return;
    run(async () => {
        const next = await api(endpoint("/review"), {
            method:"POST", headers:{"Content-Type":"application/json","X-RBPMS":"1"},
            body:JSON.stringify({action,revision:state.revision,...detail})
        });
        Object.assign(state, next);
        update();
        status(next.message ? next.message + " · 已保存" : "审核已保存");
    });
}

byId("fit").onclick = fit;
byId("actual").onclick = () => { fitted = false; center(1); };
byId("undo").onclick = () => change("undo");
byId("redo").onclick = () => change("redo");
byId("completeReview").onclick = () => change("complete");
byId("reopenReview").onclick = () => change("reopen");
byId("detectDark").onclick = () => change("detect_dark");
byId("includeDark").onclick = () => change("include_category", {category:"dark_review"});
byId("includeWeak").onclick = () => change("include_category", {category:"weak_review"});
byId("includeArtifacts").onclick = () => change("include_category", {category:"rejected_review"});
for (const input of [...filters, byId("overlay"), byId("ids"), byId("lineWidth")]) input.oninput = draw;
new ResizeObserver(() => { if (state && fitted) fit(); else draw(); }).observe(canvas);
document.addEventListener("keydown", event => {
    if (!state || !event.ctrlKey || event.target.matches("input,select,textarea")) return;
    const key = event.key.toLowerCase();
    if (!["z", "y"].includes(key)) return;
    event.preventDefault();
    change(key === "y" || event.shiftKey ? "redo" : "undo");
});

byId("import").onsubmit = event => {
    event.preventDefault();
    const form = event.currentTarget;
    run(async () => {
        const body = new FormData(form);
        for (const key of ["labels", "cells"]) {
            if (!body.get(key).name) body.delete(key);
        }
        status("正在读取原图与 V3 结果…");
        const result = await api("/api/import", {method:"POST", headers:{"X-RBPMS":"1"}, body});
        await openImage(result.id);
        await library();
        if (!result.created) status("已打开同一原图的现有审核记录");
        form.reset();
    });
};

const batch = byId("batch");
let batchRows = [], pauseRequested = false, batchRunning = false;
let previewUrl;
byId("showBatch").onclick = () => batch.showModal();
byId("closeBatch").onclick = () => batch.close();
batch.oncancel = event => { if (batchRunning) event.preventDefault(); };
byId("pauseBatch").onclick = () => {
    pauseRequested = true;
    byId("pauseBatch").disabled = true;
    byId("batchStatus").textContent = "正在完成当前图像，随后暂停";
};
byId("preview").onclose = () => {
    byId("previewImage").removeAttribute("src");
    URL.revokeObjectURL(previewUrl);
};

let batchFiles = [];
let batchDirectories = new Set(), batchReadErrors = new Map(), batchHasEmptyFolders = false;
const supportedImage = name => /\.(png|tiff?)$/i.test(name);
const imageStem = name => name.replace(/\.[^.]+$/, "").toLowerCase();
const parentFolder = path => path.slice(0, path.lastIndexOf("/"));
const excludedChannel = stem => /_(?:c2|c\d+\+\d+|dapi)$/.test(stem);

function renderBatchReport() {
    const groups = new Map();
    function group(path) {
        const key = path.split("/").slice(0, 2).join("/");
        if (!groups.has(key)) groups.set(key, {files:[], rows:[], errors:[]});
        return groups.get(key);
    }
    for (const directory of batchDirectories) if (directory.includes("/")) group(directory);
    for (const item of batchFiles) group(parentFolder(item.path)).files.push(item);
    for (const item of batchRows) group(item.folder).rows.push(item);
    for (const [path, error] of batchReadErrors) group(path).errors.push(path + " · " + error);
    let complete = 0;
    const lines = [];
    for (const [path, {files, rows, errors}] of [...groups].sort(([a], [b]) => a.localeCompare(b, "zh-CN", {numeric:true}))) {
        const finished = rows.filter(item => item.done);
        const remaining = rows.filter(item => item.check.checked && !item.done);
        const done = finished.length > 0 && !remaining.length && !errors.length;
        if (done) complete++;
        if (done && byId("batchMissingOnly").checked) continue;
        const png = files.filter(({path}) => supportedImage(path));
        const eligible = png.filter(({path}) => !excludedChannel(imageStem(path)));
        const notes = [...errors];
        if (finished.length) notes.push(finished.map(item => item.file.name + " · " + item.outcome).join("、"));
        for (const item of remaining) notes.push(item.file.name + " · " + item.result.textContent);
        if (!rows.length && !errors.length) {
            if (!files.length) notes.push("没有文件");
            else if (!png.length) notes.push("没有 PNG/TIF/TIFF · " + files.map(item => item.path.split("/").at(-1)).join("、"));
            else if (!eligible.length) notes.push("图像全部标为 DAPI 或合并通道");
            else notes.push("没有匹配后缀的图像 · " + eligible.map(item => item.path.split("/").at(-1)).join("、"));
        } else if (rows.length && !finished.length && !remaining.length) notes.push("没有勾选图片");
        const line = document.createElement("p");
        line.textContent = path + " — " + notes.join("；");
        lines.push(line);
    }
    byId("batchReportTitle").textContent = "导入检查 · 已完成 " + complete + " / " + groups.size + " 个目录" +
        (batchHasEmptyFolders ? "" : "（系统选择只显示含文件的目录）");
    byId("batchReportRows").replaceChildren(...lines);
}
byId("batchMissingOnly").onchange = renderBatchReport;
const batchDrop = byId("batchDrop");
const quickBatchDrop = byId("quickBatchDrop");
batchDrop.ondragover = quickBatchDrop.ondragover = event => {
    event.preventDefault();
    event.dataTransfer.dropEffect = busy ? "none" : "copy";
};
batchDrop.ondrop = quickBatchDrop.ondrop = async event => {
    event.preventDefault();
    if (busy) return;
    const autoStart = event.currentTarget === quickBatchDrop;
    if (!batch.open) batch.showModal();
    const directories = [...event.dataTransfer.items]
        .map(item => item.webkitGetAsEntry?.()).filter(entry => entry?.isDirectory);
    if (!directories.length) {
        byId("batchStatus").textContent = "请从文件资源管理器拖入文件夹。";
        return;
    }
    let scanned = false;
    await run(async () => {
        byId("batchStatus").textContent = "正在扫描子文件夹…";
        const found = [];
        batchDirectories = new Set();
        batchReadErrors = new Map();
        batchHasEmptyFolders = true;
        async function scan(entry) {
            const path = entry.fullPath?.slice(1);
            try {
                if (entry.isDirectory) {
                    if (path) batchDirectories.add(path);
                    const reader = entry.createReader();
                    for (;;) {
                        const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
                        if (!children.length) break;
                        for (const child of children) await scan(child);
                    }
                } else if (supportedImage(entry.name)) {
                    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
                    found.push({file, path});
                } else {
                    found.push({path});
                }
            } catch (error) {
                batchReadErrors.set(path || entry.name, "读取失败 · " + error.message);
            }
        }
        try {
            for (const directory of directories) await scan(directory);
        } catch (error) {
            byId("batchStatus").textContent = "读取拖入的文件夹失败 · " + error.message;
            return;
        }
        byId("batchFolder").value = "";
        batchFiles = found;
        scanBatch();
        scanned = batchRows.length > 0;
    });
    if (autoStart && scanned) byId("startBatch").click();
};
const pickRoot = byId("pickBatchRoot");
pickRoot.hidden = !window.showDirectoryPicker;
pickRoot.onclick = () => run(async () => {
    let root;
    try {
        root = await window.showDirectoryPicker({mode:"read"});
    } catch (error) {
        if (error.name !== "AbortError") {
            byId("batchStatus").textContent = "无法使用目录选择器，请使用旁边的「系统文件夹选择」。";
        }
        return;
    }
    byId("batchStatus").textContent = "正在扫描子文件夹…";
    const found = [];
    batchDirectories = new Set();
    batchReadErrors = new Map();
    batchHasEmptyFolders = true;
    async function scan(directory, parent) {
        batchDirectories.add(parent);
        try {
            for await (const entry of directory.values()) {
                const path = parent + "/" + entry.name;
                if (entry.kind === "directory") await scan(entry, path);
                else if (supportedImage(entry.name)) {
                    try { found.push({path, file:await entry.getFile()}); }
                    catch (error) { batchReadErrors.set(path, "读取失败 · " + error.message); }
                } else found.push({path});
            }
        } catch (error) {
            batchReadErrors.set(parent, "读取失败 · " + error.message);
        }
    }
    try {
        await scan(root, root.name);
    } catch (error) {
        byId("batchStatus").textContent = "读取文件夹失败 · " + error.message;
        return;
    }
    byId("batchFolder").value = "";
    batchFiles = found;
    scanBatch();
});

function scanBatch() {
    const suffix = byId("batchSuffix").value.trim().toLowerCase() || "_c1";
    const imageCounts = new Map();
    for (const {file, path} of batchFiles) {
        if (!file || !supportedImage(file.name)) continue;
        const folder = path.slice(0, path.lastIndexOf("/"));
        imageCounts.set(folder, (imageCounts.get(folder) ?? 0) + 1);
    }
    const files = batchFiles.filter(({file, path}) => {
        if (!file) return false;
        const stem = imageStem(file.name);
        const folder = path.slice(0, path.lastIndexOf("/"));
        return supportedImage(file.name) && (imageCounts.get(folder) === 1 ||
            (stem.endsWith(suffix) && !excludedChannel(stem)));
    }).sort((a, b) => a.path.localeCompare(b.path, "zh-CN", {numeric:true}) ||
        (a.path < b.path ? -1 : Number(a.path > b.path)));
    const folders = new Set();
    batchRows = files.map(({file, path}, index) => {
        const folder = path.split("/").slice(0, -1).join("/");
        const row = document.createElement("div");
        row.className = "batch-row";
        const check = document.createElement("input");
        check.type = "checkbox";
        check.id = "batch-file-" + index;
        check.checked = !folders.has(folder);
        folders.add(folder);
        const name = document.createElement("label");
        name.htmlFor = check.id;
        name.textContent = path;
        const channel = byId("batchChannel").cloneNode(true);
        channel.removeAttribute("id");
        channel.value = byId("batchChannel").value;
        channel.setAttribute("aria-label", "通道 " + file.name);
        const preview = document.createElement("button");
        preview.textContent = "预览";
        preview.onclick = () => run(async () => {
            let blob = file;
            if (/\.tiff?$/i.test(file.name)) {
                const body = new FormData();
                body.append("image", file);
                const response = await fetch("/api/preview", {method:"POST", headers:{"X-RBPMS":"1"}, body});
                if (!response.ok) throw new Error((await response.json()).error);
                blob = await response.blob();
            }
            previewUrl = URL.createObjectURL(blob);
            byId("previewTitle").textContent = file.name;
            byId("previewImage").src = previewUrl;
            byId("preview").showModal();
        });
        const result = document.createElement("output");
        result.textContent = check.checked ? "待计数" : "待选择";
        row.append(check, name, channel, preview, result);
        check.onchange = () => {
            if (check.checked) {
                for (const item of batchRows) {
                    if (item.folder === folder && item.check !== check && !item.done) {
                        item.check.checked = false;
                        item.result.textContent = "待选择";
                    }
                }
            }
            result.textContent = check.checked ? "待计数" : "待选择";
            renderBatchReport();
        };
        return {file, path, folder, check, channel, result, row, done:false};
    });
    byId("manageBatch").disabled = true;
    byId("batchList").replaceChildren(...batchRows.map(item => item.row));
    byId("batchStatus").textContent = files.length ? "找到 " + files.length + " 张图像，请确认清单" : "未找到匹配的 PNG/TIF/TIFF";
    renderBatchReport();
}
byId("scanBatch").onclick = scanBatch;
byId("batchFolder").onchange = event => {
    batchDirectories = new Set();
    batchReadErrors = new Map();
    batchHasEmptyFolders = false;
    batchFiles = [...event.target.files].map(file => ({file, path:file.webkitRelativePath}));
    scanBatch();
};
byId("batchChannel").onchange = event => {
    for (const item of batchRows) if (!item.done) item.channel.value = event.target.value;
};

byId("startBatch").onclick = () => run(async () => {
    const queue = batchRows.filter(item => item.check.checked && !item.done);
    if (!queue.length) {
        byId("batchStatus").textContent = "请选择尚未完成的图像";
        return;
    }
    pauseRequested = false;
    batchRunning = true;
    const locked = ["startBatch", "closeBatch", "scanBatch", "batchFolder", "pickBatchRoot", "batchSuffix", "batchChannel", "batchDark", "manageBatch"]
        .map(byId).concat(batchRows.flatMap(item => [item.check, item.channel]).filter(input => !input.disabled));
    for (const input of locked) input.disabled = true;
    byId("pauseBatch").disabled = false;
    let processed = 0, failures = 0;
    try {
        for (const item of queue) {
            if (pauseRequested) break;
            item.result.textContent = "正在计数…";
            byId("batchStatus").textContent = "正在处理 " + (processed + 1) + " / " + queue.length;
            const body = new FormData();
            body.append("image", item.file);
            body.append("channel", item.channel.value);
            body.append("image_path", item.path);
            if (byId("batchDark").checked) body.append("dark_mode", "on");
            try {
                const result = await api("/api/import", {method:"POST", headers:{"X-RBPMS":"1"}, body});
                item.done = true;
                item.id = result.id;
                item.created = result.created;
                item.outcome = result.created ? "已计数" : "已有记录";
                item.result.textContent = item.outcome;
            } catch (error) {
                failures++;
                item.result.textContent = "失败 · " + error.message;
            }
            processed++;
            renderBatchReport();
        }
        byId("batchStatus").textContent = (pauseRequested && processed < queue.length ? "已暂停 · " : "") +
            "已处理 " + processed + " / " + queue.length + "，失败 " + failures;
        const images = await library();
        const byImage = new Map(images.map(image => [image.id, image]));
        for (const item of batchRows.filter(item => item.done)) {
            const image = byImage.get(item.id);
            if (image) item.result.textContent = item.outcome + " · 自动 " + image.auto_count;
        }
        if (!state && images.length) { await openImage(images[0].id); await library(); }
    } finally {
        batchRunning = false;
        byId("pauseBatch").disabled = true;
        for (const input of locked) input.disabled = false;
        for (const item of batchRows) if (item.done) item.check.disabled = item.channel.disabled = true;
        byId("manageBatch").disabled = !batchRows.some(item => item.created);
    }
});

const manage = byId("manage");
const manageChecks = () => [...byId("manageList").querySelectorAll("input")];
function updateManageControls() {
    const checks = manageChecks();
    const selected = checks.filter(check => check.checked).length;
    byId("manageAll").checked = selected > 0 && selected === checks.length;
    byId("manageAll").indeterminate = selected > 0 && selected < checks.length;
    byId("manageApply").disabled = selected === 0;
}

async function loadManage(preselected = []) {
    const archived = byId("manageScope").value === "archived";
    const images = await api(archived ? "/api/archived" : "/api/images");
    const selected = new Set(preselected);
    byId("manageList").replaceChildren(...images.map(image => {
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = image.id;
        checkbox.checked = selected.has(image.id);
        label.append(checkbox, " " + image.image_path);
        return label;
    }));
    byId("manageApply").textContent = archived ? "恢复所选" : "移除所选";
    byId("manageStatus").textContent = images.length ? "" : "此范围没有图像";
    updateManageControls();
}

function openManage(ids = []) {
    run(async () => {
        byId("manageScope").value = "active";
        await loadManage(ids);
        manage.showModal();
    });
}
byId("manageImages").onclick = () => openManage();
byId("manageBatch").onclick = () => {
    batch.close();
    openManage(batchRows.filter(item => item.created).map(item => item.id));
};
byId("manageClose").onclick = () => manage.close();
byId("manageScope").onchange = () => run(() => loadManage());
byId("manageList").onchange = updateManageControls;
byId("manageAll").onchange = event => {
    for (const check of manageChecks()) check.checked = event.target.checked;
    updateManageControls();
};
byId("manageApply").onclick = () => run(async () => {
    const ids = manageChecks().filter(check => check.checked).map(check => check.value);
    const archived = byId("manageScope").value === "active";
    await api("/api/archive", {method:"POST", headers:{"X-RBPMS":"1", "Content-Type":"application/json"},
        body:JSON.stringify({ids, archived})});
    if (archived && ids.includes(state?.id)) {
        state = photo = undefined;
        localStorage.removeItem("rbpms-last-image");
        byId("total").textContent = "—";
        byId("counts").replaceChildren();
        byId("progress").value = 0;
        byId("reviewStatus").textContent = "";
        byId("zoom").textContent = "";
        byId("empty").hidden = false;
        draw();
    }
    const images = await library();
    if (!state && images.length) await openImage(images[0].id);
    await loadManage();
    byId("manageStatus").textContent = (archived ? "已移除 " : "已恢复 ") + ids.length + " 张图像";
});

run(async () => {
    const images = await library();
    const last = images.find(image => image.id === localStorage.getItem("rbpms-last-image")) ?? images[0];
    if (last) { await openImage(last.id); await library(); }
    else status("请选择一张原图");
});
