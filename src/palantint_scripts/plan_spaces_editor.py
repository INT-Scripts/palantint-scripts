"""Local editor for the unlabelled spaces of the historical plans.

Serves a single page on localhost: the converted plan with its automatic spaces,
where corridors, stairs, elevators and other rooms are named and classified, and
partitions close the open areas. Saving writes ``plan-spaces.json`` (a manual
source protected by the Vault) and regenerates that plan's GeoJSON.
"""

import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from palantint_scripts.config import PLAN_SPACES_PATH, SCRAPS_MANUAL_DIR
from palantint_scripts.map_gen import (
    convert_svg,
    read_plan_spaces,
    validate_plan_spaces,
    write_manifest,
    write_plan,
)

SOURCES_DIR = SCRAPS_MANUAL_DIR / "input_svgs"
PAGE = Path(__file__).with_name("plan_spaces_editor.html")


def plan_names():
    return sorted(path.stem for path in SOURCES_DIR.glob("*.svg"))


def save_definition(name, definition):
    definitions = read_plan_spaces()
    cleaned = {
        "partitions": definition.get("partitions", []),
        "spaces": [
            {key: space[key] for key in ("id", "kind", "name", "point") if space.get(key) not in (None, "")}
            for space in definition.get("spaces", [])
        ],
    }
    validate_plan_spaces(cleaned)
    if cleaned["partitions"] or cleaned["spaces"]:
        definitions[name] = cleaned
    else:
        definitions.pop(name, None)
    PLAN_SPACES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PLAN_SPACES_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(sorted(definitions.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(PLAN_SPACES_PATH)
    collection = write_plan(SOURCES_DIR / f"{name}.svg", cleaned)
    write_manifest()
    return collection


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def plan(self):
        name = unquote(self.path.removeprefix("/api/plans/"))
        if name not in plan_names():
            self.reply(HTTPStatus.NOT_FOUND, {"error": "Plan inconnu"})
            return None
        return name

    def do_GET(self):
        if self.path == "/":
            self.reply(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/plans":
            self.reply(HTTPStatus.OK, plan_names())
        elif self.path.startswith("/api/plans/"):
            if name := self.plan():
                definition = read_plan_spaces().get(name, {})
                self.reply(
                    HTTPStatus.OK,
                    {"definition": definition, "plan": convert_svg(SOURCES_DIR / f"{name}.svg", spaces=definition)},
                )
        else:
            self.reply(HTTPStatus.NOT_FOUND, {"error": "Introuvable"})

    def submitted(self, apply):
        if not self.path.startswith("/api/plans/"):
            self.reply(HTTPStatus.NOT_FOUND, {"error": "Introuvable"})
            return
        if not (name := self.plan()):
            return
        try:
            definition = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.reply(HTTPStatus.OK, apply(name, definition))
        except (ValueError, TypeError, AttributeError) as error:
            self.reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_POST(self):
        # Preview: extraction with the edited definition, nothing written.
        self.submitted(lambda name, definition: {"plan": convert_svg(SOURCES_DIR / f"{name}.svg", spaces=definition)})

    def do_PUT(self):
        self.submitted(
            lambda name, definition: {"plan": save_definition(name, definition), "definition": read_plan_spaces().get(name, {})}
        )

    def log_message(self, *args):
        pass


def main(port=8765, open_browser=True):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Éditeur des espaces de plans : {url} (Ctrl+C pour arrêter)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
