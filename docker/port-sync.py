#!/usr/bin/env python3
"""Keep qBittorrent's listening interface/port aligned with Gluetun, with authentication."""
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request



def main():
    credentials = json.loads(Path("/run/secrets/port-sync.json").read_text())
    base = f"http://127.0.0.1:{credentials.pop('port')}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    previous = None
    previous_error = None
    while True:
        try:
            try:
                with urllib.request.urlopen("http://127.0.0.1:9999/", timeout=4) as health:
                    healthy = health.status == 200
            except (OSError, urllib.error.URLError):
                healthy = False
            try:
                port = int(Path("/status/active-port").read_text().strip()) if healthy else 0
            except (OSError, ValueError):
                port = 0
            if not 0 <= port <= 65535:
                port = 0
            desired = (port, "tun0" if port else "lo")
            # Read back actual preferences too, so a restarted qBittorrent is repaired.
            try:
                with opener.open(base + "/api/v2/app/preferences", timeout=10) as response:
                    prefs = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (401, 403):
                    raise
                request = urllib.request.Request(base + "/api/v2/auth/login",
                    data=urllib.parse.urlencode(credentials).encode(), headers={"Referer": base})
                with opener.open(request, timeout=10) as response:
                    if response.status != 204:
                        raise RuntimeError("authentication failed")
                with opener.open(base + "/api/v2/app/preferences", timeout=10) as response:
                    prefs = json.load(response)
            if ((port and prefs.get("listen_port") != port)
                    or prefs.get("current_network_interface") != desired[1]):
                data = {"listen_port": port, "current_network_interface": desired[1],
                        "random_port": False, "upnp": False}
                request = urllib.request.Request(base + "/api/v2/app/setPreferences",
                    data=urllib.parse.urlencode({"json": json.dumps(data)}).encode(),
                    headers={"Referer": base})
                with opener.open(request, timeout=10):
                    pass
            if desired != previous:
                print("VPN port applied" if port else "VPN unavailable: torrent interface restricted to loopback", flush=True)
                previous = desired
            previous_error = None
        except (OSError, urllib.error.URLError, ValueError, RuntimeError):
            if previous_error is None:
                print("Waiting for authenticated qBittorrent API; Gluetun firewall remains responsible for fail-closed egress", flush=True)
            previous_error = True
        time.sleep(5)


if __name__ == "__main__":
    main()
