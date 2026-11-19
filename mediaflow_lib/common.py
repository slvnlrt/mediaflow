"""Private state, bounded HTTP calls and Compose execution shared by provisioners."""
from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class ProvisionError(RuntimeError):
    """An actionable error whose message must not contain credentials or response bodies."""
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def write_private(path: Path, content: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ProvisionError(f"Refusing symbolic-link configuration file: {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=".mediaflow-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Api:
    def __init__(self, base_url: str, *, label: str = "", headers: dict | None = None):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ProvisionError("Invalid API base URL")
        self.base_url = base_url.rstrip("/")
        self.label = label or parsed.hostname or "API"
        self.headers = dict(headers or {})
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect()
        )

    def request(self, method: str, path: str, payload=None, *, form=None,
                headers=None, allowed=(200, 201, 202, 204)):
        if not path.startswith("/") or path.startswith("//"):
            raise ProvisionError("API paths must be relative to the configured service")
        request_headers = {"Accept": "application/json", **self.headers, **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(self.base_url + path, data=body,
                                         headers=request_headers, method=method)
        # Never include the response body, query parameters or request data in errors.
        safe_path = path.split("?", 1)[0]
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            if exc.code not in allowed:
                raise ProvisionError(f"{self.label}: HTTP {exc.code} ({method} {safe_path})", status=exc.code) from None
            response = exc
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProvisionError(f"{self.label}: service unreachable ({method} {safe_path})") from None
        with response:
            if response.status not in allowed:
                raise ProvisionError(f"{self.label}: HTTP {response.status} ({method} {safe_path})", status=response.status)
            content = response.read()
        if not content:
            return None
        text = content.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def wait(self, path: str, timeout: int = 180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.request("GET", path)
            except ProvisionError:
                if time.monotonic() >= deadline:
                    raise ProvisionError(f"{self.label}: not ready after {timeout}s") from None
                time.sleep(2)


CONTAINER_PORTS = {
    "qbittorrent": 8080, "radarr": 7878, "sonarr": 8989, "prowlarr": 9696,
    "jellyfin": 8096, "seerr": 5055, "qui": 7476, "wud": 3000, "byparr": 8191,
}


class Context:
    def __init__(self, project: Path, config: dict, host: str = "127.0.0.1"):
        self.root = Path(project).resolve()
        self.cfg = config
        self.state = self.root / ".state"
        self.host = host

    def api(self, service: str, *, headers=None) -> Api:
        published = self.cfg.get("managed", {}).get("published_services", [])
        host = self.host
        if self.cfg.get("phase") == "provisioning" and service not in published:
            host = "127.0.0.1"
        return Api(f"http://{host}:{self.cfg['ports'][service]}", label=service, headers=headers)

    def container_port(self, service: str) -> int:
        # qBittorrent validates Host's port even behind a Docker port mapping.
        return self.cfg["ports"][service] if service == "qbittorrent" else CONTAINER_PORTS[service]

    def internal(self, service: str) -> str:
        host = service
        if self.cfg["vpn"]["mode"] != "none" and service in {"qbittorrent", "prowlarr", "byparr"}:
            host = "gluetun"
        return f"http://{host}:{self.container_port(service)}"

    def save(self) -> None:
        write_private(self.state / "config.json", json.dumps(self.cfg, indent=2) + "\n")

    def compose(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        command = ["docker", "compose", "--project-name", self.cfg["project_name"],
                   "--file", str(self.state / "compose.json"), *args]
        try:
            result = subprocess.run(command, cwd=self.root, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise ProvisionError("Cannot execute Docker Compose") from None
        if result.returncode:
            # Compose output can expand private environment variables. Keep it private.
            write_private(self.state / "last-compose-error.log", result.stdout + result.stderr)
            raise ProvisionError("Docker Compose failed; private diagnostic: .state/last-compose-error.log")
        return result

    def note(self, message: str) -> None:
        print(message, flush=True)
