import assert from "node:assert/strict";
import {spawn, spawnSync} from "node:child_process";
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync} from "node:fs";
import {join, resolve} from "node:path";
import {setTimeout as delay} from "node:timers/promises";

const temp = mkdtempSync(resolve(".browser-review-"));
const hosted = process.argv.includes("--hosted");
const python = spawn(resolve(".venv/Scripts/python.exe"), hosted
    ? ["-u","-m","http.server","0","--bind","127.0.0.1","--directory","share_web/dist"]
    : ["rbpms_web.py", "--port", "0", "--project", join(temp,"project")]);
const chrome = spawn(process.env.RBPMS_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", [
    "--headless", "--no-proxy-server", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0",
    "--window-size=1500,1000", "--user-data-dir=" + join(temp,"chrome"), "about:blank"
], {windowsHide:true});
let ws;
function line(stream, pattern) {
    return new Promise((ok, fail) => {
        let output = "";
        const timer = setTimeout(() => fail(new Error(output || "进程启动超时")), 20000);
        stream.on("data", chunk => {
            output += chunk;
            const match = output.match(pattern);
            if (match) { clearTimeout(timer); ok(match[1]); }
        });
    });
}
try {
    const [url, port] = await Promise.all([
        line(python.stdout, /(http:\/\/127\.0\.0\.1:\d+\/)/),
        line(chrome.stderr, /DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/)
    ]);
    const targets = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    const target = targets.find(item => item.type === "page");
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise(ok => ws.addEventListener("open", ok, {once:true}));
    let serial = 0;
    const pending = new Map();
    ws.addEventListener("message", event => {
        const packet = JSON.parse(event.data);
        if (!pending.has(packet.id)) return;
        const {ok, fail} = pending.get(packet.id);
        pending.delete(packet.id);
        if (packet.error) fail(new Error(packet.error.message));
        else ok(packet.result);
    });
    function send(method, params = {}) {
        return new Promise((ok, fail) => {
            const id = ++serial;
            pending.set(id, {ok, fail});
            ws.send(JSON.stringify({id,method,params}));
        });
    }
    async function evalPage(expression) {
        if (hosted && expression.includes("fetch(")) {
            expression = `import('/local-request.js').then(({localRequest: fetch}) => (${expression}))`;
        }
        const response = await send("Runtime.evaluate", {expression, awaitPromise:true, returnByValue:true});
        if (response.exceptionDetails) throw new Error(response.exceptionDetails.text);
        return response.result.value;
    }
    async function waitFor(expression) {
        for (let i = 0; i < (hosted ? 600 : 100); i++) {
            if (await evalPage(expression)) return;
            if (hosted) {
                const failure = await evalPage("document.querySelector('#status')?.textContent");
                if (/Error:|无法启动浏览器/.test(failure ?? "")) throw new Error(failure);
            }
            await delay(100);
        }
        throw new Error("等待失败 " + expression + " " + await evalPage("document.body.innerText"));
    }
    const navigation = await send("Page.navigate", {url});
    if (navigation.errorText) throw new Error(navigation.errorText + " " + url);
    await waitFor("document.querySelector('#status')?.textContent === '请选择一张原图'");
    const doc = await send("DOM.getDocument");
    const sample = readdirSync("images").find(name => name.endsWith("_c1.png"));
    async function file(name, path) {
        const currentDoc = await send("DOM.getDocument");
        const {nodeId} = await send("DOM.querySelector", {nodeId:currentDoc.root.nodeId, selector:"input[name=" + name + "]"});
        await send("DOM.setFileInputFiles", {nodeId,files:[resolve(path)]});
    }
    await file("image", join("images",sample));
    await file("labels", join("results_v3", sample.replace(".png","_v3_labels16.png")));
    await file("cells", join("results_v3", sample.replace(".png","_v3_cells.csv")));
    await evalPage("document.querySelector('#importButton').click()");
    await waitFor("!document.querySelector('#exports').hidden");
    const read = () => evalPage("fetch(document.querySelector('#csv').href.replace('/count.csv','')).then(r=>r.json())");
    const initial = await read();
    assert.deepEqual([initial.width,initial.height],[1024,1024]);
    assert.match(initial.source,/导入/);
    const count = () => evalPage("Number(document.querySelector('#total').textContent)");
    assert.equal(await count(), initial.included.length);
    const box = await evalPage("document.querySelector('canvas').getBoundingClientRect().toJSON()");
    const fit = Math.min(box.width / 1024, box.height / 1024);
    const center = [box.x + box.width / 2, box.y + box.height / 2];
    const [cx,cy] = center;
    function mouse(type,x,y,more={}) {
        return send("Input.dispatchMouseEvent", {type,x,y,...more});
    }
    const click = async (x,y) => {
        await mouse("mousePressed",x,y,{button:"left",clickCount:1});
        await mouse("mouseReleased",x,y,{button:"left",clickCount:1});
    };
    await mouse("mouseWheel",cx,cy,{deltaX:0,deltaY:-120});
    await mouse("mouseWheel",cx,cy,{deltaX:0,deltaY:120});
    await mouse("mouseWheel",cx,cy,{deltaX:0,deltaY:-120});
    await mouse("mousePressed",cx,cy,{button:"left",clickCount:1});
    await mouse("mouseMoved",cx+23,cy+17,{button:"left",buttons:1});
    await mouse("mouseReleased",cx+23,cy+17,{button:"left",clickCount:1});
    function screen(x,y) {
        return [cx+23+(x+.5-512)*fit*1.2, cy+17+(y+.5-512)*fit*1.2];
    }
    const candidate = initial.cells.find(cell => initial.included.includes(cell.id) &&
        cell.x > 150 && cell.x < 850 && cell.y > 150 && cell.y < 850);
    await click(...screen(Math.floor(candidate.x), Math.floor(candidate.y)));
    const n = initial.included.length;
    await waitFor("Number(document.querySelector('#total').textContent)===" + (n-1));
    assert.equal((await read()).decisions[candidate.id], false);
    const points = Array.from({length:100}, (_, i) => [240+i%10*55,240+Math.floor(i/10)*55]);
    const [x,y] = points.find(([x,y]) => initial.cells.every(cell => Math.hypot(cell.x-x,cell.y-y)>40));
    await click(...screen(x,y));
    await waitFor("Number(document.querySelector('#total').textContent)===" + n);
    assert.deepEqual(Object.values((await read()).added),[[x,y]]);
    async function shortcut(key) {
        await send("Input.dispatchKeyEvent",{type:"rawKeyDown",modifiers:2,key});
        await send("Input.dispatchKeyEvent",{type:"keyUp",modifiers:2,key});
    }
    await shortcut("z");
    await waitFor("Number(document.querySelector('#total').textContent)===" + (n-1));
    await shortcut("y");
    await waitFor("Number(document.querySelector('#total').textContent)===" + n);
    await send("Page.reload");
    await waitFor("document.querySelector('#exports')?.hidden === false");
    const saved = await read();
    assert.deepEqual(Object.values(saved.added),[[x,y]]);
    assert.equal(saved.decisions[candidate.id],false);
    const csv = () => evalPage("fetch(document.querySelector('#csv').href).then(r=>r.text())");
    assert.equal(await csv(), `文件夹名,自动计数,人工审核后计数\r\n${sample},${n},\r\n`);
    await evalPage("document.querySelector('#completeReview').click()");
    await waitFor("document.querySelector('#reviewStatus').textContent.startsWith('已完成审核')");
    await send("Page.reload");
    await waitFor("document.querySelector('#reviewStatus')?.textContent.startsWith('已完成审核')");
    assert.equal(await csv(), `文件夹名,自动计数,人工审核后计数\r\n${sample},${n},${n}\r\n`);
    assert.ok(await evalPage("fetch(document.querySelector('#png').href).then(async r=>r.ok && (await r.arrayBuffer()).byteLength > 100)"));
    const root = join(temp, "批次");
    const folder = join(root, "样本", "左眼");
    mkdirSync(folder, {recursive:true});
    const fixture = spawnSync(resolve(".venv/Scripts/python.exe"), ["-c", [
        "import cv2, numpy as np, sys",
        "from pathlib import Path",
        "from rbpms_counter_v3 import save_png",
        "from PIL import Image",
        "rgb = np.zeros((128,128,3), np.uint8)",
        "cv2.circle(rgb,(45,50),12,(220,0,0),-1)",
        "for name in ('视网膜_c2.png','视网膜_c1+2.png'): save_png(Path(sys.argv[1])/name,rgb)",
        "for name in ('视网膜_c1.TIFF','视网膜2_c1.tif'): Image.fromarray(rgb).save(Path(sys.argv[1])/name,compression='tiff_lzw')"
    ].join("\n"), folder], {encoding:"utf8"});
    assert.equal(fixture.status, 0, fixture.stderr);
    const fixtureFiles = readdirSync(folder).map(name => ({name, data: Array.from(readFileSync(join(folder,name)))}));
    await evalPage(`(() => {
        const dir = (children, fullPath) => ({isDirectory:true, fullPath, createReader(){
            let next = children;
            return {readEntries(done){ const entries = next; next = []; done(entries); }};
        }});
        const files = ${JSON.stringify(fixtureFiles)}.map(({name,data}) => ({
            name, fullPath:'/批次/样本/左眼/' + name,
            file(done){done(new File([new Uint8Array(data)],name,{type:'image/png'}));}
        }));
        const target = document.querySelector('#quickBatchDrop');
        target.ondrop({preventDefault(){}, currentTarget:target,
            dataTransfer:{items:[{webkitGetAsEntry:()=>dir([
                dir([dir(files, '/批次/样本/左眼')], '/批次/样本'),
                dir([], '/批次/空目录')
            ], '/批次')}]}});
    })()`);
    await waitFor("document.querySelectorAll('.batch-row').length === 2");
    assert.equal(await evalPage("document.querySelectorAll('.batch-row input:checked').length"), 1);
    assert.match(await evalPage("document.querySelector('.batch-row label').textContent"), /批次\/样本\/左眼\/视网膜_c1\.TIFF/);
    await waitFor("document.querySelector('#batchStatus').textContent === '已处理 1 / 1，失败 0'");
    await waitFor("document.querySelector('#batchReportTitle').textContent.includes('1 / 2')");
    assert.match(await evalPage("document.querySelector('#batchReportRows').textContent"), /空目录.*没有文件/);
    const batchImages = await evalPage("fetch('/api/images').then(r=>r.json())");
    assert.equal(batchImages.length, 2);
    assert.equal(batchImages[0].channel, "red");
    assert.equal(batchImages[0].auto_count, 1);
    assert.equal(batchImages[0].image_path, "批次/样本/左眼/视网膜_c1.TIFF");
    const summary = await evalPage("fetch('/api/summary.csv').then(r=>r.text())");
    assert.match(summary, /左眼,1,\r\n/);
    await evalPage(`(() => {
        document.querySelector('#closeBatch').click();
        const list = document.querySelector('#images');
        for (let i = 0; i < 80; i++) {
            const entry = list.firstElementChild.cloneNode(true);
            entry.removeAttribute('data-image-id');
            entry.textContent = '批量图像 ' + i + ' · 视网膜铺片样本';
            list.prepend(entry);
        }
        list.scrollTop = list.scrollHeight;
        list.querySelectorAll('[data-image-id]')[0].click();
    })()`);
    await waitFor("Number(document.querySelector('#total').textContent) === 1");
    const layout = await evalPage(`(() => {
        const list = document.querySelector('#images');
        const canvas = document.querySelector('canvas');
        const box = canvas.getBoundingClientRect();
        const scale = Math.min(canvas.width, canvas.height) / 128;
        const x = Math.floor((canvas.width - 128 * scale) / 2 + 45 * scale);
        const y = Math.floor((canvas.height - 128 * scale) / 2 + 50 * scale);
        return {scrollable:list.scrollHeight > list.clientHeight, listScrolled:list.scrollTop > 0,
            pageScroll:scrollY, imageInView:box.top >= 0 && box.bottom <= innerHeight,
            red:canvas.getContext('2d').getImageData(x,y,1,1).data[0]};
    })()`);
    assert.ok(layout.scrollable && layout.listScrolled && layout.imageInView, JSON.stringify(layout));
    assert.equal(layout.pageScroll, 0);
    assert.ok(layout.red > 150, JSON.stringify(layout));
    await evalPage("document.querySelector('#showBatch').click(); document.querySelector('#manageBatch').click()");
    await waitFor("document.querySelector('#manage').open && !document.querySelector('#manageApply').disabled");
    assert.deepEqual(await evalPage("[...document.querySelectorAll('#manageList input:checked')].map(x=>x.value)"), [batchImages[0].id]);
    await evalPage("document.querySelector('#manageApply').click()");
    await waitFor("document.querySelector('#manageStatus').textContent === '已移除 1 张图像'");
    assert.ok(!(await evalPage("fetch('/api/summary.csv').then(r=>r.text())")).includes("视网膜_c1.png"));
    await evalPage("const scope = document.querySelector('#manageScope'); scope.value='archived'; scope.dispatchEvent(new Event('change'))");
    await waitFor("document.querySelector('#manageApply').textContent === '恢复所选'");
    await evalPage("document.querySelector('#manageAll').click(); document.querySelector('#manageApply').click()");
    await waitFor("document.querySelector('#manageStatus').textContent === '已恢复 1 张图像'");
    assert.match(await evalPage("fetch('/api/summary.csv').then(r=>r.text())"), /左眼,1,/);
    await evalPage("document.querySelector('#manageClose').click()");
    const gray = join(temp, '16bit.tif');
    const grayFixture = spawnSync(resolve('.venv/Scripts/python.exe'), ['-c',
        "import numpy as np, cv2, sys; from PIL import Image; x=np.zeros((128,128),np.uint16); cv2.circle(x,(45,50),12,56540,-1); cv2.circle(x,(100,100),12,10280,-1); Image.fromarray(x).save(sys.argv[1],compression='tiff_lzw')", gray], {encoding:'utf8'});
    assert.equal(grayFixture.status, 0, grayFixture.stderr);
    await evalPage("document.querySelector('[name=labels]').value=''; document.querySelector('[name=cells]').value=''; document.querySelector('[name=channel]').value='red'; document.querySelector('[name=dark_mode]').checked=true");
    await file('image', gray);
    await evalPage("document.querySelector('#importButton').click()");
    await waitFor("document.querySelector('#status').textContent.includes('16bit.tif') && !document.querySelector('#exports').hidden");
    assert.equal(await count(), 1);
    assert.equal((await read()).cells.filter(c => c.category === 'dark_review').length, 1);
    await evalPage("document.querySelector('#includeDark').click()");
    await waitFor("Number(document.querySelector('#total').textContent) === 2");
    await evalPage("document.querySelector('#undo').click()");
    await waitFor("Number(document.querySelector('#total').textContent) === 1");
    await evalPage("document.querySelector('#redo').click()");
    await waitFor("Number(document.querySelector('#total').textContent) === 2");
    const tiffBytes = Array.from(readFileSync(gray));
    assert.ok(await evalPage(`(async () => {
        const body = new FormData(); body.append('image',new File([new Uint8Array(${JSON.stringify(tiffBytes)})],'preview.tif'));
        const r = await fetch('/api/preview',{method:'POST',headers:{'X-RBPMS':'1'},body});
        if (!r.ok) throw new Error(await r.text());
        const bitmap = await createImageBitmap(await r.blob());
        return bitmap.width === 128 && bitmap.height === 128;
    })()`));
    console.log("Browser: review, CSV and PNG exports, red batch count, and viewport with a long image list passed");
} finally {
    ws?.close();
    for (const child of [python, chrome]) child.kill();
    await delay(1500);
    rmSync(temp,{recursive:true,force:true,maxRetries:5,retryDelay:250});
}
