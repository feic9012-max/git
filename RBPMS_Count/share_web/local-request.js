const pending = new Map();
let worker, serial = 0;

const access = new Promise((resolve, reject) => {
    if (!navigator.locks) {
        reject(new Error("请使用最新版 Edge 或 Chrome 打开 HTTPS 网页"));
        return;
    }
    navigator.locks.request("rbpms-review", {ifAvailable:true}, async lock => {
        if (!lock) {
            reject(new Error("请先关闭其他已打开的 RBPMS 审核标签页，再刷新本页"));
            return;
        }
        resolve();
        await new Promise(() => {});
    }).catch(reject);
});

export async function localRequest(url, options) {
    await access;
    if (!worker) {
        worker = new Worker("/review-worker.js", {type:"module"});
        worker.onmessage = ({data}) => {
            const task = pending.get(data.id);
            if (!task) return;
            pending.delete(data.id);
            if (data.error) task.reject(new Error(data.error));
            else task.resolve(new Response(data.bytes, {status:data.status, headers:{"Content-Type":data.mime}}));
        };
        worker.onerror = () => {
            for (const task of pending.values()) task.reject(new Error("无法启动浏览器计数引擎"));
            pending.clear();
            worker.terminate();
            worker = undefined;
        };
    }
    const request = new Request(new URL(url, location.origin), options);
    const body = request.body ? await request.arrayBuffer() : new ArrayBuffer(0);
    const id = ++serial;
    return new Promise((resolve, reject) => {
        pending.set(id, {resolve,reject});
        worker.postMessage({id, method:request.method, path:new URL(request.url).pathname,
            contentType:request.headers.get("Content-Type") ?? "", body}, [body]);
    });
}
