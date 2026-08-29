"""
printd.py - the print side of the split, as a small HTTP service.

One job: turn a 4x6 PDF into marks on paper. It owns the printer and the
printer's configuration, and it knows nothing about orders, buyers,
SQLite or IMAP. That separation is the whole point - `printers.py` is the
only code in this system verified against real hardware, and everything
around it churns.

**Deploying this is gated on open-work item 1 being signed off.** Do not
change the print path while the print path is unvalidated: `gap_inches`
is still ASSUMED, and confounding a new transport with an unmeasured
label geometry means a failure you cannot attribute.

What crosses the wire, and what deliberately does not:

  in    the *stamped* 4x6 PDF - the exact bytes cli.print_label already
        builds - plus a job id, an HMAC, a protocol version and how long
        the caller is willing to wait.
  out   whether it printed, and why not.
  never printer_dpi, darkness, speed, gap_inches, media. Those are facts
        about the hardware and the roll of stock in the room, and they
        stay on the machine being tuned. Shipping pre-rendered bytes
        instead of a PDF would move them off it, which is exactly wrong
        during the week they change daily.

No wall-clock timestamp in the signature. A Pi has no battery-backed
clock and restores time from the last shutdown until timesyncd catches
up, so the first print after a power cut - the case this whole design
exists for - would fail a timestamp window. It would present as `401 bad
signature`, which reads unmistakably as a wrong secret, and the night
would be spent regenerating a key that was never wrong. Staleness is
handled by a monotonic deadline instead, and replay by a durable
journal; neither needs a synchronised clock.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import build as build_mod
from . import printers

log = logging.getLogger("mplabel.printd")

PROTOCOL = "1"
MAX_BODY = 8 * 1024 * 1024
DEFAULT_PORT = 9101

# How many job records to keep. Enough to answer "did that batch print?"
# long after the fact, small enough that the file stays trivial.
JOURNAL_KEEP = 5000


def sign(secret, job, body):
    """HMAC over the job id and a digest of the body.

    The body is covered so a truncated or swapped PDF is rejected rather
    than printed - the failure being guarded against is a parcel posted
    to the wrong person, and a label is just bytes."""
    digest = hashlib.sha256(body).hexdigest()
    return hmac.new((secret or "").encode(),
                    f"{job}\n{digest}".encode(), hashlib.sha256).hexdigest()


class Journal:
    """Append-only record of what actually reached the printer.

    Durable on purpose. An in-memory set would forget across a restart,
    and a restart is precisely what follows a printer fault. This is also
    what converts the irreducible ambiguity of a timed-out print - did it
    come out or not? - from "go and look" into a query she can run from
    the kitchen."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._done = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for line in self.path.read_text(errors="replace").splitlines():
                try:
                    self._done.add(json.loads(line)["job"])
                except (ValueError, KeyError):
                    continue

    def seen(self, job):
        with self._lock:
            return job in self._done

    def record(self, job, nbytes, digest):
        # ts is informational only. The Pi may have no correct clock yet;
        # nothing here depends on it being right.
        row = {"job": job, "ts": time.time(), "bytes": nbytes,
               "sha256": digest}
        with self._lock:
            self._done.add(job)
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
            self._trim()
        return row

    def _trim(self):
        try:
            lines = self.path.read_text(errors="replace").splitlines()
        except OSError:
            return
        if len(lines) <= JOURNAL_KEEP * 2:
            return
        self.path.write_text("\n".join(lines[-JOURNAL_KEEP:]) + "\n")

    def since(self, job=None, limit=200):
        try:
            lines = self.path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if job:
            for i, row in enumerate(rows):
                if row.get("job") == job:
                    rows = rows[i + 1:]
                    break
        return rows[-limit:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mplabel-printd"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # --- plumbing

    def _send(self, status, payload=None):
        body = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, status, message):
        self._send(status, {"error": message})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _check(self, body):
        """Protocol, then signature. Returns the job id or None (answered)."""
        if self.headers.get("X-MPLabel-Protocol") != PROTOCOL:
            self.fail(400, f"unsupported protocol; this printd speaks {PROTOCOL}")
            return None
        job = self.headers.get("X-MPLabel-Job") or ""
        got = self.headers.get("X-MPLabel-Sig") or ""
        want = sign(self.server.secret, job, body)
        if not job or not hmac.compare_digest(got, want):
            log.warning("rejected job %r from %s: bad signature",
                        job, self.address_string())
            self.fail(401, "bad signature")
            return None
        return job

    def _deadline(self):
        raw = self.headers.get("X-MPLabel-Deadline")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    # --- dispatch

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.h_health()
        if path == "/printed":
            return self.h_printed()
        self.fail(404, "no such endpoint")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/print":
                return self.h_print()
            if path == "/selftest":
                return self.h_selftest()
            self.fail(404, "no such endpoint")
        except ValueError as exc:
            self.fail(400, str(exc))
        except BrokenPipeError:
            log.debug("caller went away")
        except BaseException as exc:
            # BaseException on purpose. printers.send still raises
            # SystemExit for an unknown backend, and socketserver only
            # catches Exception - a daemon that dies on a printer fault,
            # restarts, and dies again on the retry is worse than one that
            # answers 503 and stays up to be asked why.
            log.exception("print failed")
            self.fail(503, f"{type(exc).__name__}: {exc}")

    # --- handlers

    def h_health(self):
        """Never touches the device and never takes the print lock.

        That is the whole point of it. A wedged write - out of paper, lid
        open, a gap distance the printer cannot find - must leave this
        answering, or the one command you would run to diagnose the
        problem hangs in exactly the case it exists for. `printing_since`
        is how a wedge becomes visible rather than silent."""
        srv = self.server
        device = srv.cfg.get("printer_device")
        since = srv.printing_since
        self._send(200, {
            "ok": True,
            "backend": srv.cfg.get("printer_backend"),
            "device": device,
            "device_present": bool(device) and Path(device).exists(),
            "dpi": srv.cfg.get("printer_dpi"),
            "darkness": srv.cfg.get("printer_darkness"),
            "speed": srv.cfg.get("printer_speed"),
            "media_tracking": srv.cfg.get("media_tracking"),
            "gap_inches": srv.cfg.get("gap_inches"),
            "head_dots": srv.cfg.get("printer_head_dots"),
            "printing": since is not None,
            "printing_for": (round(time.monotonic() - since, 1)
                             if since is not None else None),
            "build": build_mod.stamp(),
            "protocol": PROTOCOL,
        })

    def h_printed(self):
        qs = parse_qs(urlparse(self.path).query)
        after = (qs.get("since") or [None])[0]
        self._send(200, {"printed": self.server.journal.since(after)})

    def h_print(self):
        body = self._read_body()
        job = self._check(body)
        if job is None:
            return
        if not body:
            return self.fail(400, "empty body")
        if not body.startswith(b"%PDF"):
            return self.fail(400, "body is not a PDF")

        if self.server.journal.seen(job):
            # Already printed. Say so plainly rather than printing twice:
            # a duplicate label on a parcel is a real cost.
            return self._send(409, {"printed": False, "job": job,
                                    "error": "job already printed"})

        deadline = self._deadline()
        with self.server.device(deadline) as got:
            if not got:
                return self._send(410, {
                    "printed": False, "job": job,
                    "error": f"could not reach the printer within "
                             f"{deadline}s; another job was ahead of it"})
            tmp = self.server.spool / f"{job}.pdf"
            tmp.write_bytes(body)
            try:
                backend = self.server.cfg["printer_backend"]
                printers.send(str(tmp), backend,
                              **printers.backend_kwargs(self.server.cfg,
                                                        backend))
            finally:
                tmp.unlink(missing_ok=True)

        row = self.server.journal.record(job, len(body),
                                         hashlib.sha256(body).hexdigest())
        log.info("printed %s (%d bytes)", job, len(body))
        self._send(200, {"printed": True, "job": job, "bytes": row["bytes"],
                         "backend": self.server.cfg.get("printer_backend")})

    def h_selftest(self):
        body = self._read_body()
        if self._check(body) is None:
            return
        cfg = self.server.cfg
        with self.server.device(self._deadline()) as got:
            if not got:
                return self.fail(410, "printer busy")
            printers.tspl_selftest(cfg["printer_device"],
                                   cfg.get("media_tracking", "gap"),
                                   float(cfg.get("gap_inches", 0.12)))
        self._send(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The kernel completes handshakes for connections nobody has accepted
    # yet, which turns the backlog into an invisible queue that prints
    # unattended minutes later. Keep it at one and let callers fail fast.
    request_queue_size = 1

    def __init__(self, addr, cfg):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.secret = cfg.get("printd_secret") or ""
        state = Path(cfg.get("printd_state_dir")
                     or Path(cfg.get("home", ".")) / "printd")
        self.spool = state / "spool"
        self.spool.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(state / "done.jsonl")
        self._gate = threading.Lock()
        self.printing_since = None

    def device(self, deadline):
        """Exclusive use of the printer, or a clean refusal.

        Threaded rather than single-threaded, because single-threading
        would also serialise `/healthz` behind a wedged write - and the
        lock is the serialisation, not the thread count. Bounded rather
        than blocking, because a caller that has already given up should
        not have its label printed to an empty room ten minutes later."""
        return _Device(self, deadline)


class _Device:
    def __init__(self, server, deadline):
        self.server = server
        self.deadline = deadline
        self.held = False

    def __enter__(self):
        if not self.server._gate.acquire(timeout=max(0.05, self.deadline)):
            return False
        # The flock as well: another process on this Pi - a hand-run
        # `mplabel reprint` over ssh - reaches the same printer.
        self._lock = printers.print_lock(self.server.cfg, required=True)
        self._lock.__enter__()
        self.held = True
        self.server.printing_since = time.monotonic()
        return True

    def __exit__(self, *exc):
        if self.held:
            self.server.printing_since = None
            try:
                self._lock.__exit__(*exc)
            finally:
                self.server._gate.release()
        return False


def serve(cfg, bind=None, port=None):
    bind = bind or cfg.get("printd_bind", "127.0.0.1")
    port = int(port or cfg.get("printd_port", DEFAULT_PORT))
    if not cfg.get("printd_secret"):
        raise SystemExit(
            "printd_secret is not set, and this refuses to accept unsigned "
            "print jobs.\nGenerate one with `python3 -c \"import secrets; "
            "print(secrets.token_hex(32))\"` and put the same value in the "
            "config on both sides.")
    httpd = Server((bind, port), cfg)
    log.info("printd on http://%s:%d, backend %s, device %s",
             bind, port, cfg.get("printer_backend"), cfg.get("printer_device"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
