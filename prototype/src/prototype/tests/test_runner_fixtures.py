"""The trusted HTTP client must return redirects without following them."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from prototype.service_tools.runner.fixtures import api_client


def test_redirect_is_returned_even_when_following_is_requested():
    visited = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302 if self.path == "/" else 200)
            if self.path == "/":
                self.send_header("Location", "/target")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    fixture = api_client.__wrapped__(url)
    session = next(fixture)
    try:
        assert session.get(url).status_code == 302
        assert session.get(url, allow_redirects=True).status_code == 302
        assert visited == ["/", "/"]
    finally:
        fixture.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
