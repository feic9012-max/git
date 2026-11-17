import {loadPyodide} from "/py-runtime/pyodide.mjs";

const runtime = new URL("/py-runtime/", self.location.href).href;
let ready;

function sync(fs, load) {
    return new Promise((resolve, reject) => fs.syncfs(load, error => error ? reject(error) : resolve()));
}

async function initialize() {
    const python = await loadPyodide({indexURL:runtime});
    await python.loadPackage(["opencv-python", "scikit-image"]);
    const fs = python.FS;
    fs.mkdir("/review-data");
    fs.mount(fs.filesystems.IDBFS, {}, "/review-data");
    await sync(fs, true);
    for (const name of ["rbpms_web", "rbpms_counter_v2", "rbpms_counter_v3", "rbpms_review_gui", "browser_api"]) {
        const response = await fetch(name + ".py");
        if (!response.ok) throw new Error("无法加载 " + name);
        fs.writeFile(name + ".py", await response.text());
    }
    return {fs, request:python.runPython("from browser_api import request\nrequest")};
}

let queue = Promise.resolve();
self.onmessage = ({data}) => {
    queue = queue.then(async () => {
        const {id, method, path, contentType, body} = data;
        try {
            ready ??= initialize();
            const {fs, request} = await ready;
            const result = request(method, path, contentType, new Uint8Array(body));
            let reply;
            try {
                const [status, mime, filename, bytes] = result.toJs();
                reply = {id,status,mime,filename,bytes};
            } finally {
                result.destroy();
            }
            if (method === "POST" && reply.status < 400) await sync(fs, false);
            self.postMessage(reply, [reply.bytes.buffer]);
        } catch (error) {
            self.postMessage({id,error:String(error)});
        }
    });
};

