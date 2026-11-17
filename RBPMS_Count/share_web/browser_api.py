"""Adapt the local HTTP API to in-browser requests without a network server."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from rbpms_web import Handler, Project

project = Project(Path("/review-data"))


class BrowserHandler(Handler):
    def __init__(self, method, path, content_type, body):
        self.command = method
        self.path = path
        self.headers = {"Host": "127.0.0.1:8765", "X-RBPMS": "1",
                        "Content-Type": content_type, "Content-Length": str(len(body))}
        self.rfile = BytesIO(body)
        self.server = SimpleNamespace(project=project, server_port=8765)

    def respond(self, body, mime, status=200, filename=None):
        self.result = (status, mime, filename, body)


def request(method, path, content_type, body):
    handler = BrowserHandler(method, path, content_type, bytes(body))
    handler.dispatch()
    return handler.result
