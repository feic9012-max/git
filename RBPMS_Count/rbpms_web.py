"""Local browser adapter for the existing V3 counter and desktop review model."""
import argparse
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import webbrowser

import cv2
from rbpms_counter_v2 import select_channel
from rbpms_counter_v3 import classify_image, read_png
from rbpms_review_gui import ReviewModel

STATIC = Path(__file__).with_name("web_review")
LIMIT = 64 * 1024 * 1024


class Project:
    def __init__(self, path):
        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                    history TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY, image_id TEXT NOT NULL REFERENCES images(id),
                    timestamp TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {column["name"] for column in db.execute("PRAGMA table_info(images)")}
            for column in ("image_path", "channel", "segmentation_dir"):
                if column not in columns:
                    db.execute(f"ALTER TABLE images ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "reviewed_count" not in columns:
                db.execute("ALTER TABLE images ADD COLUMN reviewed_count INTEGER")
            if "review_reopened" not in columns:
                db.execute("ALTER TABLE images ADD COLUMN review_reopened INTEGER NOT NULL DEFAULT 0")
            if "archived" not in columns:
                db.execute("ALTER TABLE images ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path / "review.sqlite3", timeout=60)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def model(self, image_id, segmentation_dir=None):
        if segmentation_dir is None:
            with self.connect() as db:
                segmentation_dir = self.row(db, image_id)["segmentation_dir"]
        folder = self.path / "images" / image_id
        return ReviewModel(folder / "source.png", folder / segmentation_dir)

    def ingest(self, name, original, labels=None, cells=None, *, channel="green", image_path=""):
        if not name.lower().endswith(".png") or not original.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("请选择 8-bit PNG 原图")
        if (labels is None) != (cells is None):
            raise ValueError("导入 V3 结果时需要同时选择标签 PNG 和候选 CSV")
        if channel not in ("auto", "red", "green"):
            raise ValueError("请选择自动、红色或绿色通道")
        image_path = image_path or name
        if ("\\" in image_path or any(part in ("", ".", "..") for part in image_path.split("/"))
                or image_path.split("/")[-1] != name or len(image_path) > 4096):
            raise ValueError("无效的图像相对路径")
        image_id = hashlib.sha256(original).hexdigest()
        folder = self.path / "images" / image_id
        folder.parent.mkdir(exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.row(db, image_id)
            if existing:
                if existing["archived"]:
                    saved_path = image_path if "/" in image_path else existing["image_path"] or image_path
                    db.execute("""UPDATE images SET archived=0,image_path=?,revision=revision+1
                                  WHERE id=?""", (saved_path, image_id))
                    return image_id, False
                if "/" in image_path and "/" not in existing["image_path"]:
                    db.execute("UPDATE images SET image_path=? WHERE id=?", (image_path, image_id))
                saved_channel = self.channel(existing)
                if labels is None and saved_channel:
                    if channel == "auto":
                        channel = select_channel(read_png(folder / "source.png"), channel)
                    if saved_channel != channel:
                        self.correct_empty_channel(db, existing, folder, channel)
                        return image_id, True
                return image_id, False
            with TemporaryDirectory(dir=folder.parent) as temporary:
                staging = Path(temporary) / "image"
                staging.mkdir()
                image = staging / "source.png"
                image.write_bytes(original)
                if labels is None:
                    channel = classify_image(image, staging, channel=channel)["channel"]
                else:
                    channel = ""
                    (staging / "source_v3_labels16.png").write_bytes(labels)
                    (staging / "source_v3_cells.csv").write_bytes(cells)
                model = ReviewModel(image, staging)
                for row in model.candidates.values():
                    x, y = float(row["x_px"]), float(row["y_px"])
                    if (not (0 <= x < model.w and 0 <= y < model.h)
                            or row["class"] not in CLASSES):
                        raise ValueError("候选坐标或分类无效")
                # A prior interruption may leave a complete folder before SQLite commits.
                if folder.exists():
                    for file in staging.iterdir():
                        file.replace(folder / file.name)
                else:
                    staging.rename(folder)
                source = "V3 导入" if labels is not None else "V3 生成"
                db.execute("""INSERT INTO images(id,name,source,history,image_path,channel)
                              VALUES (?,?,?,?,?,?)""",
                           (image_id, name, source, json.dumps([self.snapshot(model, {})]), image_path, channel))
        return image_id, True

    def correct_empty_channel(self, db, row, folder, channel):
        history = json.loads(row["history"])
        current = history[row["cursor"]]
        previous = self.model(row["id"], row["segmentation_dir"])
        if previous.candidates or current["added"]:
            raise Conflict("此原图已有候选或人工计入点。请用另一个项目比较不同通道。")
        # Keep the existing files readable until the database switches to the new result.
        version = f"segmentation-{row['revision'] + 1}-{channel}"
        target = folder / version
        with TemporaryDirectory(dir=folder) as temporary:
            staging = Path(temporary) / "result"
            image = folder / "source.png"
            classify_image(image, staging, channel=channel)
            model = ReviewModel(image, staging)
            if not model.candidates:
                raise Conflict("所选通道也没有候选，已保留原审核记录。")
            model.next_id = current["next_id"]
            if target.exists():
                for file in staging.iterdir():
                    file.replace(target / file.name)
            else:
                staging.rename(target)
        db.execute("""UPDATE images SET channel=?,segmentation_dir=?,history=?,cursor=0,reviewed_count=NULL,
                      revision=revision+1 WHERE id=?""",
                   (channel, version, json.dumps([self.snapshot(model, {})]), row["id"]))
        detail = dict(previous_channel=self.channel(row), channel=channel, history=history,
                      cursor=row["cursor"], segmentation_dir=row["segmentation_dir"])
        db.execute("INSERT INTO audit(image_id,timestamp,action,detail) VALUES (?,?,?,?)",
                   (row["id"], datetime.now(timezone.utc).isoformat(), "correct_channel", json.dumps(detail)))

    @staticmethod
    def row(db, image_id):
        return db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()

    @staticmethod
    def channel(row):
        return row["channel"] or ("green" if row["source"] == "V3 生成" else "")

    def summaries(self, archived=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM images WHERE archived=? ORDER BY rowid DESC",
                              (int(archived),)).fetchall()
        results = []
        for row in rows:
            history = json.loads(row["history"])
            initial, current = history[0], history[row["cursor"]]
            decisions = current["decisions"]
            results.append(dict(
                id=row["id"], name=row["name"], image_path=row["image_path"] or row["name"],
                source=row["source"], channel=self.channel(row), algorithm_version="V3",
                auto_count=len(initial["included"]), manual_added=len(current["added"]),
                manual_removed=len(set(initial["included"]) - set(current["included"])),
                manual_confirmed=len(set(current["included"]) - set(initial["included"])),
                final_count=len(current["included"]) + len(current["added"]),
                reviewed_count=row["reviewed_count"],
                reviewed_candidates=len(decisions),
            ))
        return results

    def archive(self, ids, archived):
        if (not isinstance(ids, list) or not ids or type(archived) is not bool
                or any(not isinstance(id_, str) or not re.fullmatch(r"[a-f0-9]{64}", id_) for id_ in ids)):
            raise ValueError("请选择要移除或恢复的图像")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("UPDATE images SET archived=?,revision=revision+1 WHERE id=? AND archived<>?",
                           ((int(archived), id_, int(archived)) for id_ in ids))

    def summary_csv(self, image_id=None):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(("文件夹名", "自动计数", "人工审核后计数"))
        summaries = self.summaries()
        if image_id is not None:
            summaries = [summary for summary in summaries if summary["id"] == image_id]
            if not summaries:
                raise FileNotFoundError("图像不存在")
        for summary in summaries:
            parts = summary["image_path"].split("/")
            name = parts[-2] if len(parts) > 1 else summary["name"]
            if name.lstrip().startswith(("=", "+", "-", "@")):
                name = "'" + name
            writer.writerow((name, summary["auto_count"], summary["reviewed_count"]))
        return stream.getvalue().encode("utf-8-sig")

    @staticmethod
    def snapshot(model, decisions):
        return dict(included=sorted(model.included), added={key: list(point) for key, point in model.added.items()},
                    next_id=model.next_id, decisions=decisions.copy())

    def review(self, image_id, change=None, contours=False, export=None):
        with self.connect() as db:
            if change is not None:
                db.execute("BEGIN IMMEDIATE")
            row = self.row(db, image_id)
            if row is None or row["archived"]:
                raise FileNotFoundError("图像不存在")
            model = self.model(image_id, row["segmentation_dir"])
            history = json.loads(row["history"])
            cursor, revision = row["cursor"], row["revision"]
            reviewed_count = row["reviewed_count"]
            reopened = bool(row["review_reopened"])
            state = history[cursor]
            message = ""
            if change is not None:
                if change.get("revision") != revision:
                    raise Conflict("此图已在另一个页面更新，请重新打开")
                action = change.get("action")
                detail = None
                if action == "edit":
                    x, y = change.get("x"), change.get("y")
                    mode = change.get("mode")
                    if (type(x) is not int or type(y) is not int or
                            not (0 <= x < model.w and 0 <= y < model.h) or
                            mode not in ("toggle", "add", "remove")):
                        raise ValueError("无效的审核操作")
                    self.restore(model, state)
                    message = model.edit_at(x, y, mode)
                    if model.events:
                        detail = model.events[-1]
                    elif mode == "add" and model.locate_candidate(x, y) in model.included:
                        idx = model.locate_candidate(x, y)
                        if str(idx) not in state["decisions"]:
                            detail = {"action": "confirm_candidate", "id": idx}
                    if detail:
                        decisions = state["decisions"].copy()
                        if isinstance(detail["id"], int):
                            decisions[str(detail["id"])] = detail["action"] != "exclude_candidate"
                        history = history[:cursor + 1] + [self.snapshot(model, decisions)]
                        cursor += 1
                elif action == "include_category":
                    category = change.get("category")
                    if category not in ("weak_review", "rejected_review"):
                        raise ValueError("无效的候选分类")
                    self.restore(model, state)
                    added = [idx for idx, cell in model.candidates.items()
                             if cell["class"] == category and idx not in model.included]
                    message = f"已批量纳入 {len(added)} 个候选"
                    if added:
                        model.included.update(added)
                        decisions = state["decisions"].copy()
                        decisions.update({str(idx): True for idx in added})
                        history = history[:cursor + 1] + [self.snapshot(model, decisions)]
                        cursor += 1
                        detail = {"category": category, "ids": added}
                elif action == "undo" and cursor > 0:
                    cursor -= 1
                    detail, message = {"cursor": cursor}, "已撤销"
                elif action == "redo" and cursor + 1 < len(history):
                    cursor += 1
                    detail, message = {"cursor": cursor}, "已重做"
                elif action == "complete":
                    reviewed_count = len(state["included"]) + len(state["added"])
                    message = f"审核已完成 · 已记录 {reviewed_count} 个细胞"
                    if row["reviewed_count"] is None or reopened:
                        detail = {"reviewed_count": reviewed_count}
                        reopened = False
                elif action == "reopen" and reviewed_count is not None and not reopened:
                    reopened = True
                    detail, message = {"reviewed_count": reviewed_count}, "已恢复审核"
                elif action not in ("undo", "redo", "reopen"):
                    raise ValueError("未知操作")
                if detail:
                    revision += 1
                    if action != "complete" and not reopened:
                        if reviewed_count is not None:
                            message += " · 请重新完成审核"
                        reviewed_count = None
                    db.execute("""UPDATE images SET history=?,cursor=?,revision=?,reviewed_count=?,
                                  review_reopened=? WHERE id=?""",
                               (json.dumps(history), cursor, revision, reviewed_count, int(reopened), image_id))
                    db.execute("INSERT INTO audit(image_id,timestamp,action,detail) VALUES (?,?,?,?)",
                               (image_id, datetime.now(timezone.utc).isoformat(), action, json.dumps(detail)))
                state = history[cursor]
            self.restore(model, state)
            if export == "png":
                return self.overlay(model)
            if export == "csv":
                return self.csv_bytes(row, model, state)
            result = dict(id=image_id, name=row["name"], source=row["source"], channel=self.channel(row), width=model.w,
                          height=model.h, revision=revision, canUndo=cursor > 0,
                          canRedo=cursor + 1 < len(history), reviewed_count=reviewed_count,
                          review_reopened=reopened, message=message, **state)
            if contours:
                result["cells"] = [
                    dict(id=idx, x=float(cell["x_px"]), y=float(cell["y_px"]),
                         category=cell["class"], polygons=[p[:, 0, :].tolist() for p in model.contours[idx]])
                    for idx, cell in model.candidates.items()
                ]
            return result

    @staticmethod
    def restore(model, state):
        model.included = set(state["included"])
        model.added = state["added"].copy()
        model.next_id = state["next_id"]

    @staticmethod
    def overlay(model):
        model.rebuild_overlay()
        ok, png = cv2.imencode(".png", cv2.cvtColor(model.view, cv2.COLOR_RGB2BGR))
        if not ok:
            raise OSError("无法编码标注 PNG")
        return png.tobytes()

    @staticmethod
    def csv_bytes(row, model, state):
        stream = io.StringIO(newline="")
        fields = ["image_id", "image_name", "algorithm_version", "channel", "id", "x_px", "y_px",
                  "class", "included", "review_status", "area_px2", "mean_signal", "max_signal", "local_contrast",
                  "circularity", "solidity", "vessel_overlap", "review_note"]
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        name = row["name"]
        if name.lstrip().startswith(("=", "+", "-", "@")):
            name = "'" + name
        channel = Project.channel(row)
        image = dict(image_id=row["id"], image_name=name, algorithm_version="V3", channel=channel)
        for idx, cell in model.candidates.items():
            decision = state["decisions"].get(str(idx))
            status = "未审核" if decision is None else ("人工确认" if decision else "人工排除")
            writer.writerow(dict(cell, **image, included=int(idx in model.included), review_status=status,
                                 mean_signal=cell.get("mean_" + channel), max_signal=cell.get("max_" + channel)))
        for mid, (x, y) in model.added.items():
            writer.writerow(dict(image, id=mid, x_px=x, y_px=y,
                                 included=1, review_status="人工新增", **{"class": "manual"}))
        return stream.getvalue().encode("utf-8-sig")


CLASSES = {"strong_candidate", "weak_review", "rejected_review"}


class Conflict(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    def respond(self, body, mime, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.respond(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def dispatch(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != origin.removeprefix("http://"):
            return self.send_json({"error": "请使用启动窗口中显示的本地地址"}, 403)
        project = self.server.project
        path = urlsplit(self.path).path
        try:
            if self.command == "POST":
                if self.headers.get("X-RBPMS") != "1" or self.headers.get("Origin") not in (None, origin):
                    return self.send_json({"error": "请求来源无效"}, 403)
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= LIMIT:
                    raise ValueError("上传内容为空或超过 64 MB")
                body = self.rfile.read(size)
                if path == "/api/archive":
                    change = json.loads(body)
                    if not isinstance(change, dict):
                        raise ValueError("无效的图像管理请求")
                    project.archive(change.get("ids"), change.get("archived"))
                    return self.send_json({"ok": True})
                if path == "/api/import":
                    content_type = self.headers.get("Content-Type", "")
                    envelope = BytesParser(policy=policy.default).parsebytes(
                        f"Content-Type: {content_type}\r\n\r\n".encode() + body)
                    if not envelope.is_multipart():
                        raise ValueError("无效的图像上传")
                    files = {part.get_param("name", header="content-disposition"): part
                             for part in envelope.iter_parts()}
                    image = files.get("image")
                    if image is None or not image.get_filename():
                        raise ValueError("请选择原图")
                    name = image.get_filename().replace("\\", "/").split("/")[-1]
                    def field(key, default):
                        part = files.get(key)
                        return part.get_payload(decode=True).decode("utf-8") if part is not None else default
                    image_id, created = project.ingest(
                        name, image.get_payload(decode=True),
                        *(files[key].get_payload(decode=True) if key in files else None
                          for key in ("labels", "cells")),
                        channel=field("channel", "green"), image_path=field("image_path", name))
                    return self.send_json(dict(id=image_id, created=created))
                match = re.fullmatch(r"/api/images/([a-f0-9]{64})/review", path)
                if match:
                    change = json.loads(body)
                    if not isinstance(change, dict):
                        raise ValueError("无效的审核请求")
                    return self.send_json(project.review(match[1], change))
            else:
                if path in ("/", "/app.js", "/style.css"):
                    filename, mime = {
                        "/": ("index.html", "text/html; charset=utf-8"),
                        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                        "/style.css": ("style.css", "text/css; charset=utf-8"),
                    }[path]
                    return self.respond((STATIC / filename).read_bytes(), mime)
                if path == "/api/images":
                    return self.send_json(project.summaries())
                if path == "/api/archived":
                    return self.send_json(project.summaries(archived=True))
                if path == "/api/summary.csv":
                    return self.respond(project.summary_csv(), "text/csv; charset=utf-8", filename="summary.csv")
                match = re.fullmatch(r"/api/images/([a-f0-9]{64})(/source\.png|/review\.png|/cells\.csv|/count\.csv)?", path)
                if match:
                    image_id, suffix = match.groups()
                    if suffix == "/source.png":
                        return self.respond((project.path / "images" / image_id / "source.png").read_bytes(), "image/png")
                    if suffix == "/review.png":
                        return self.respond(project.review(image_id, export="png"), "image/png", filename="review.png")
                    if suffix == "/cells.csv":
                        return self.respond(project.review(image_id, export="csv"), "text/csv; charset=utf-8", filename="cells.csv")
                    if suffix == "/count.csv":
                        return self.respond(project.summary_csv(image_id), "text/csv; charset=utf-8", filename="count.csv")
                    return self.send_json(project.review(image_id, contours=True))
            return self.send_json({"error": "页面不存在"}, 404)
        except Conflict as error:
            self.send_json({"error": str(error)}, 409)
        except FileNotFoundError as error:
            self.send_json({"error": str(error)}, 404)
        except (ValueError, KeyError, TypeError, cv2.error) as error:
            self.send_json({"error": str(error)}, 400)

    do_GET = dispatch
    do_POST = dispatch


def serve(path, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.project = Project(path)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RBPMS 本地网页审核")
    parser.add_argument("--project", type=Path, default=Path(__file__).with_name("review_project"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="在 Windows 默认浏览器打开")
    args = parser.parse_args()
    with serve(args.project, args.port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(url, flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()


