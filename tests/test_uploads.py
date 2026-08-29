from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from webcompanion import uploads
from webcompanion.uploads import handle, images_ok, UPLOAD_MAX_BYTES


def make_handler(headers, body=b""):
    h = MagicMock()
    h.headers = headers
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    return h


def test_unsupported_media_type(tmp_path):
    h = make_handler({"Content-Type": "text/plain", "Content-Length": "1"}, b"x")
    handle(h, {"state_dir": tmp_path})
    h.send_response.assert_called_with(415)


def test_missing_content_length(tmp_path):
    h = make_handler({"Content-Type": "image/png"})
    handle(h, {"state_dir": tmp_path})
    h.send_response.assert_called_with(411)


def test_payload_too_large(tmp_path):
    big = str(UPLOAD_MAX_BYTES + 1)
    h = make_handler({"Content-Type": "image/png", "Content-Length": big})
    handle(h, {"state_dir": tmp_path})
    h.send_response.assert_called_with(413)


def test_invalid_content_length(tmp_path):
    h = make_handler({"Content-Type": "image/png", "Content-Length": "abc"})
    handle(h, {"state_dir": tmp_path})
    h.send_response.assert_called_with(400)


def test_happy_path(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    h = make_handler({"Content-Type": "image/png", "Content-Length": str(len(png))}, png)
    handle(h, {"state_dir": tmp_path})
    h.send_response.assert_called_with(200)
    images = list((tmp_path / "images").iterdir())
    assert len(images) == 1
    assert images[0].suffix == ".png"


# ── images_ok: containment against a hostile or buggy client ──────────────

def test_images_ok_accepts_a_real_file_under_the_images_dir(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    f = images_dir / "abc.png"
    f.write_bytes(b"x")
    assert images_ok([{"path": str(f)}], tmp_path) is True


def test_images_ok_accepts_an_empty_list(tmp_path):
    assert images_ok([], tmp_path) is True


def test_images_ok_rejects_a_path_outside_the_images_dir(tmp_path):
    # A client naming e.g. /etc/passwd as a "pasted image" must be refused —
    # this is the containment check that stops Claude being told to read it.
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    assert images_ok([{"path": str(outside)}], tmp_path) is False


def test_images_ok_rejects_traversal_out_of_the_images_dir(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (tmp_path / "secret.png").write_bytes(b"x")
    traversal = str(images_dir / ".." / "secret.png")
    assert images_ok([{"path": traversal}], tmp_path) is False


def test_images_ok_rejects_a_missing_file(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    missing = images_dir / "nope.png"
    assert images_ok([{"path": str(missing)}], tmp_path) is False


def test_images_ok_rejects_malformed_entries(tmp_path):
    assert images_ok("not-a-list", tmp_path) is False
    assert images_ok([{"path": ""}], tmp_path) is False
    assert images_ok([{"nopath": "x"}], tmp_path) is False
    assert images_ok(["not-a-dict"], tmp_path) is False


def test_a_client_that_declares_bytes_it_never_sends_does_not_park_a_thread(tmp_path):
    """`rfile.read(length)` blocks until every declared byte arrives. A
    client is free to declare 10 MB and send one, and with no timeout that
    client holds a daemon thread forever -- in a process where every open
    tab and IDE client already holds one for the life of its stream.

    Uses a real socket pair and `conn.makefile("rb")`, which is exactly what
    BaseHTTPRequestHandler uses for `rfile`: the timeout has to reach the
    SOCKET, and a BytesIO stand-in cannot show that it does.
    """
    import socket
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()
    client.sendall(b"x")  # one byte of the megabyte it claims

    handler = make_handler({"Content-Type": "image/png",
                            "Content-Length": "1048576"})
    handler.connection = conn
    handler.rfile = conn.makefile("rb")

    original = uploads.UPLOAD_TIMEOUT_SECONDS
    uploads.UPLOAD_TIMEOUT_SECONDS = 0.3
    started = time.monotonic()
    try:
        uploads.handle(handler, {"state_dir": str(tmp_path)})
    finally:
        uploads.UPLOAD_TIMEOUT_SECONDS = original
        client.close()
        conn.close()
        server.close()

    elapsed = time.monotonic() - started
    assert elapsed < 5, f"the handler blocked {elapsed:.1f}s past its timeout"
    handler.send_response.assert_called_with(408)
    assert not list(tmp_path.glob("images/*")), "a partial upload was saved"


def test_a_complete_upload_over_a_real_socket_still_succeeds(tmp_path):
    """The negative control: the timeout must not break the ordinary path,
    and the socket's original timeout must be restored."""
    import socket

    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    conn, _ = server.accept()
    conn.settimeout(None)
    client.sendall(png)

    handler = make_handler({"Content-Type": "image/png",
                            "Content-Length": str(len(png))})
    handler.connection = conn
    handler.rfile = conn.makefile("rb")
    try:
        uploads.handle(handler, {"state_dir": str(tmp_path)})
        assert conn.gettimeout() is None, "the socket timeout was not restored"
    finally:
        client.close()
        conn.close()
        server.close()

    handler.send_response.assert_called_with(200)
    saved = list(tmp_path.glob("images/*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == png
