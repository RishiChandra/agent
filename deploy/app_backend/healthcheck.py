"""Cheap HTTP liveness; model/DB checks run separately before deployment."""
import os
import sys
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main():
    try:
        port = int(os.environ.get("PORT", "8000"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            return 0 if 200 <= response.status < 300 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
