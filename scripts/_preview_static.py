"""Minimal static file server for previewing static/index.html.

Serves the repo's static/ directory on a fixed port using an absolute path, so
it does not depend on the process working directory (the preview sandbox denies
os.getcwd()). Dev-only helper — not part of the app.
"""
import http.server
import os
import socketserver

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(os.path.dirname(_HERE), "static")
_PORT = 8137


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=_STATIC, **kwargs)


if __name__ == "__main__":
    with socketserver.TCPServer(("127.0.0.1", _PORT), Handler) as httpd:
        print(f"serving {_STATIC} at http://127.0.0.1:{_PORT}")
        httpd.serve_forever()
