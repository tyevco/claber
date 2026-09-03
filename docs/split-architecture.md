I verified every load-bearing claim against the source. All the fatal/serious findings from the adversarial pass reproduce. Here is the plan.

---

# Splitting mplabel: order side and print side

## 1. Context — does the premise hold?

**Partly. The print-path split is worth doing. The k3s move is not, yet, and it is a separate decision.**

You framed this as "I might have to split this app". Two different things are bundled in that sentence, and they have different answers.

**What you probably wanted, and already have.** If the goal is "work the queue from my phone from anywhere", that is done. `web.py` exists and `*.personal.in.ottercoders.dev` already routes to this machine's cloudflared tunnel. Adding a rule for the Pi's `mplabel serve` port gets you the phone app today, with zero new moving parts. Nothing needs splitting for that.

**What the split genuinely buys.** One thing, and it is real: it decouples "the machine that decides what to print" from "the machine with the printer plugged into it". Today those are the same SD card. That matters because `printers.py` is the only code in this system verified on real hardware, and everything else — IMAP, SQLite, `listings.refresh()`, Sheets, the PWA — is churn. A print agent whose entire job is `bytes → /dev/usb/lp0` is a thing you can stop touching. That is worth having independently of where the order side runs.

**What the k3s move additionally buys, and what it costs.** It buys blast-radius reduction (the Gmail app password and the Sheets service-account key leave the Pi) and it stops the SD card being the only copy of the database. It costs, concretely:

- A new silent parcel-loss mode. `candidate_ids` (cli.py:268) searches `newer_than:{lookback_days}d`, default 7. Today a powered-off Pi loses nothing — mail waits in Gmail and the next poll catches up. Once the poller is in the cluster, a cluster outage longer than the window drops mail with **no error and no record anywhere**. Every candidate design proposed "raise it to 30 and add a warning", and that warning lives inside the component whose absence it is meant to detect. It does not work.
- A wrong-parcel hazard on rollback. Every design says "keep the Pi's copy as the rollback artefact". That leaves a bootable stale `sales.db`, a full `labels/` archive, and a working CLI on the Pi. CLAUDE.md open-work item 7 tells the maintainer that `mplabel pending` is the way back when a label did not print. Run on the Pi after a cutover, that command finds every pre-cutover row still `printed_at IS NULL` and reprints labels for parcels that shipped weeks ago — and `allocate_code` (cli.py:359) scopes taken codes to non-closed statuses, so those stale 3-char codes have since been reissued to live parcels.
- A config split-brain. `load_config` gives `MPLABEL_<KEY>` env vars precedence over the file (cli.py:163-167). Every k8s sketch sets config as env. So the Pi stays file-authoritative and the pod becomes env-authoritative, and editing a mounted `/etc/mplabel.conf` in the pod does nothing, silently — the same shape as the config trap CLAUDE.md already documents.
- Clock dependence on a machine with no RTC (see §5, C4).

**Recommendation:** do the print split. Keep the order side on the Pi. Revisit k3s only when the precondition in Phase 5 is met. This is not a compromise position — it is where the value is, and stopping there permanently is a good outcome.

**One hard sequencing rule that overrides all of this:** CLAUDE.md open-work item 1 — does one job advance exactly one die-cut label, is the ink creeping, do the barcodes scan — is still open. `gap_inches = 0.12` is marked ASSUMED and has never been measured against her stock. **Do not change the print path while the print path is unvalidated.** Phase 1 below is a bug-fix phase that helps item 1; Phase 2 is a diagnostic that helps item 1; the transport split is Phase 3 and is gated on item 1 being signed off.

---

## 2. Recommended architecture

**Design 1 (`pi-http` + `mplabel printd`), with the corrections in §5.**

```
┌─ Raspberry Pi ────────────────────────────────────────────┐
│  mplabel.service      run --loop   (IMAP, SQLite, labels/) │
│  mplabel.service      serve        (PWA, via cloudflared)  │
│  mplabel-printd.service            (HTTP :9101 → printer)  │
│         ↑ loopback today, LAN later                        │
└────────────────────────────────────────────────────────────┘
```

The order side reaches the printer through a new `pi-http` entry in `printers.BACKENDS`. `cli.print_label()` stays the single choke point; `cmd_reprint`, `cmd_pending`, `cmd_test_print` and `web._print_one` are untouched. Switching is one line in `/etc/mplabel.conf`; rolling back is `printer_backend = tspl` and a restart.

### What crosses the wire

**Order side → Pi.** `POST /print`, `Content-Type: application/pdf`. Body is the **stamped throwaway 4x6 PDF** — the exact bytes `print_label` already builds at cli.py:511-518 (~2-4 KB). Headers:

| Header | Meaning |
|---|---|
| `X-MPLabel-Job` | `{code}-{16 hex nonce}`, fresh per attempt |
| `X-MPLabel-Sig` | hex HMAC-SHA256 over `job + "\n" + sha256hex(body)`, keyed on `printd_secret` |
| `X-MPLabel-Protocol` | `1`; printd rejects unknown with 400 |
| `X-MPLabel-Deadline` | integer seconds of client patience (see C2) |
| `X-MPLabel-Override` | optional, signed, one-shot `darkness=10,gap_inches=0.15` — never persisted |

**No wall-clock timestamp.** See §5 C4 — the Pi has no RTC and a timestamp window is a self-inflicted total print outage after a power cut.

**Pi → order side.** `200 {"printed":true,"job":...,"bytes":N,"backend":"tspl"}`, returned only after `_write_raw` returns *and its settle sleep has elapsed*. Plus `401` bad signature, `409` job already in the done journal, `410` deadline exceeded before the write started, `400` protocol/geometry rejection, `503` printer unavailable, `500` otherwise with exception text.

**Also crossing:** `GET /healthz` (unauthenticated, prints nothing, never touches the device) → backend, device presence, resolved `printer_dpi`/`darkness`/`speed`/`gap_inches`/`media_tracking`, build stamp, pypdfium2 and Pillow versions, and whether a print is currently in flight. `GET /printed?since=<job-id|iso>` → the done journal. `POST /selftest` (signed).

**What deliberately does not cross:** no order id, no buyer, no `label_pdf` path, no database, and **no printer config**. `printer_dpi`, `printer_darkness`, `printer_speed`, `media_tracking`, `gap_inches`, `settle_seconds` are facts about the hardware and the roll of stock in the room. They stay in `/etc/mplabel.conf` on the Pi — which is exactly where open-work item 1 needs them.

**Why the PDF and not pre-rendered TSPL bytes.** Shipping job bytes would make the Pi lighter, but it moves `dpi`/`darkness`/`gap` off the machine being tuned, breaks the CUPS backends (which need a *file* for `lp`), and requires changing `printers.py` — the only code verified on this hardware. Rendering stays on the Pi. This also structurally eliminates the entire class of wrong-dpi bug that Design 2 needed a profile-exchange protocol to defend against: the dpi never leaves the machine that owns it.

---

## 3. CUPS — answered

**Yes, CUPS stays supported. No, the G4 does not go behind it.**

Nothing to build. `printers.BACKENDS` already has `cups-pdf` and `cups-raster`; `install_pi.sh:16-18` apt-installs `cups cups-client`; `systemd/mplabel.service:3` is already `After=cups.service`. Because `printd` calls `printers.send()` unmodified, setting `printer_backend = cups-pdf` in the Pi's config keeps working. That is genuine CUPS support for a second or future printer at zero cost, and it is the honest reading of your request.

**Do not put the G4 behind CUPS, and do not use IPP as the transport.** Four reasons, in order of how much they would hurt:

1. **CUPS claiming the printer unbinds usblp**, killing the one path verified on this hardware. `install_pi.sh:54-59` adds `usblp` to `/etc/modules` specifically to prevent this, and `_write_raw`'s own error message (printers.py:298-302) explains it. The counter-proposal — a custom backend under an `mplabel://` URI scheme, which CUPS would dispatch instead of the libusb `usb` backend — is *plausible* and **has never been executed on this unit**. Putting an unverified path in front of the only verified one, while the label geometry is still unvalidated, confounds two variables on the one part of the system currently known-good.
2. **"Printed" would become a text-scrape.** `lp` exit 0 means *queued*, not printed. The only route to printed is polling `lpstat` and parsing its output — locale-sensitive, not an API. This project has already recorded a good label as NOT PRINTED once (the fsync/EINVAL incident, printers.py:309-319). Making that the primary mechanism reintroduces the failure class.
3. **`printer-error-policy` has no good setting.** `abort-job` leaves the queue disabled after one failure, needing `cupsenable g4` typed over ssh — she cannot do that from the post office, and the phone app surfaces no queue state. `retry-job` can silently reprint after a failure that happened *after* the bytes went out.
4. **Chunking.** CUPS backends copy in 8-64 KB chunks with no per-job delay. This firmware discards bytes arriving while the head is moving. That is the exact hazard `_write_raw` was written to avoid.

**One live CUPS hazard to fix now, independent of any split:** `printers.probe()` ends with an unconditional `subprocess.run(["sudo", "lpinfo", "-v"])` (printers.py:420-422, confirmed present). That invokes CUPS's libusb backend in discovery mode — the diagnostic command is a live path to unbinding usblp on a working system. Gate it behind `--cups`.

---

## 4. NATS — answered

**No, and I would not add it later either, except for notifications.**

The honest paragraph: NATS solves message durability, and this system does not have a message-durability problem. The durable queue already exists and is authoritative — `printed_at IS NULL AND status NOT IN CLOSED_STATUSES AND label_pdf IS NOT NULL` is the query behind both `cmd_pending` (cli.py:852) and `web.h_pending` (web.py:467), and `mark_printed` is the commit. A JetStream stream would be a *second* durable store shadowing the one that `mplabel list`, `pending` and the phone app all read, and when they disagree there is no answer to which is true. Three further specifics: `nats-py` is asyncio-only against a codebase that is blocking everywhere (`imaplib`, `fcntl.flock`, `ThreadingHTTPServer`, a `_write_raw` that ends in `time.sleep`), so it costs an event loop or a rewrite of the print path; the cluster's existing broker at 10.0.1.102:14222 accepts unauthenticated plaintext connections from the whole LAN and serves five other apps, and label PDFs carry buyers' home addresses, so reusing it is a privacy regression and fixing it is a five-app blast radius; and at-least-once redelivery on the print path means duplicate labels, plus the ability to replay a job for an order that has since been cancelled — something the SQLite queue structurally cannot do, because `printed_at` and `status` are re-read at send time.

There is one genuine point in NATS's favour that deserves recording: **it dials outbound**, so it needs no inbound port on the Pi. If the Pi ever turns out not to be reachable from wherever the order side runs, that is the question to revisit — and even then the stdlib answer is a long-poll `GET /api/next-job` held open by the Pi, not a broker.

And one genuine point the adversarial pass found in NATS's favour that nobody credited: publishing returns immediately, so a batch print cannot outlive Cloudflare's 100-second edge timeout. That is a real problem (§5 C6) — but it has a cheaper fix than a broker.

**Where a bus would earn its place, if you want one:** fire-and-forget notifications — `label.printed`, `print.failed`, `order.new` — published from the order side only, nothing on the path between a sale and a printed label. Optional, still a new dependency, and not part of this plan.

---

## 5. Corrections that must land first

The adversarial pass landed 22 findings. These are the ones that are real, and every one of them is a bug **in the code as it stands today** or a hole the split would open. Grouped by whether they gate the split.

### Group A — bugs in today's code. Fix regardless of whether you ever split.

**A1 (fatal). `SystemExit` bypasses every error handler in the system.**
`printers._write_raw` raises `SystemExit` when the device node is missing (printers.py:298) — i.e. the printer is switched off, unplugged, or CUPS grabbed it. `SystemExit` derives from `BaseException`, not `Exception` (verified). So it escapes *every* handler this system relies on: cli.py:589, cli.py:836, cli.py:898, web.py:599, and `web._dispatch`'s catch-all at web.py:400-407. Consequences today:

- The poller **dies** rather than logging "print failed". `Restart=always`/`RestartSec=30` restarts it, and because `upsert()` runs before the print attempt, each restart makes exactly one message of progress. Nine threaded label emails — the scenario CLAUDE.md records — is nine crashes over four and a half minutes.
- From the phone, `SystemExit` escapes `ThreadingMixIn.process_request_thread`'s `except Exception` and is **swallowed silently by `threading`** (verified). She gets a bare connection close: no 500, no error text, no note. And `h_print_pending` abandons the whole batch on the first row, directly contradicting its own docstring at web.py:613.

The handler that every design's failure story is built on has almost certainly never fired for a physical printer fault.

**Fix:** add `class PrinterUnavailable(Exception)` to `printers.py` and raise that from `_write_raw` instead. Keep the message verbatim. In `cli.main()`, catch it at top level and `raise SystemExit(str(exc))` so CLI ergonomics are unchanged. Add a regression test that asserts `_write_raw` on a missing device raises something `except Exception` catches.

**A2 (fatal, and only partly fixable). A successful `os.write` does not mean a label emerged.**
Out of paper, head open, jam, misfeed, and a wrong `gap_inches` all produce a *successful* write: `_write_raw` pushes 124 KB into the printer's buffer in one burst and the printer only then discovers there is nothing to print on. `printers.py` never reads from the device. So `print_label` returns normally, `mark_printed` sets `printed_at`, and the row **leaves** the Pending query. The one physical failure that actually loses a parcel is the one that makes the parcel invisible to the recovery mechanism.

This is true today, and it is unchanged by every design. It is also getting worse in a way nobody flagged: the PWA is precisely the feature that lets her print from the kitchen or the post office, so "a human looks at the printer" — the last line of defence in every proposal — stops being true at the moment this feature ships.

**Partial fix, and the highest-value experiment in this plan.** `printd` is the right place to ask the printer how it is. TSPL exposes `<ESC>!?` (one status byte) and `~HS`. If the G4 answers, `printd` refuses the job with 503 on paper-out or head-open, and the row stays in Pending where she can see it. **This is ASSUMED — bidirectional reads from `/dev/usb/lp0` have never been tested on this unit, and a blocking read is its own wedge risk.** Test it standalone (§7) before wiring it in, always with a short `select()` timeout and a fall-through to "unknown, print anyway". If it does not answer, record that as a finding and accept at-least-once — but do not skip the experiment, because it is the only thing here that converts a silent parcel loss into a visible one.

**A5 (serious). The geometry guard everyone proposed checks the wrong thing.**
Asserting the received PDF is 4.00x6.00in is checking a property already enforced upstream by `label._snap` and locked by `test_output_is_exactly_4x6`. The physical hazard is one layer lower: `render_bitmap` (printers.py:60-78) pins the dot count to `round(LABEL_W_IN * dpi)` with **no comparison against the 812-dot print head** — confirmed, there is no such check anywhere in `printers.py`. A perfectly valid 4x6 PDF with `printer_dpi = 300` (a plausible edit — printers.py:26 says "some printers are 300" and `mplabel.conf.example` invites the value) renders 1200 dots onto an 812-dot head. That is CLAUDE.md's own 824-dot lesson at four times the overflow.

**Fix:** add `printer_head_dots` to `DEFAULTS` (default `812`) and a hard check in `render_bitmap`: if `want_w > head_dots`, raise with the dpi and the head width in the message. Test it. Do the 4x6 page-size assertion in `printd` too — it is three lines and it is the last gate before paper — but understand it is the cheap half.

**A12 (serious). The stamped temp file collides between threads.**
`print_label` builds `mplabel_{code}_{os.getpid()}.pdf` (cli.py:511-513) — keyed on code and PID only. `web.Server` is a `ThreadingHTTPServer` (web.py:672), so two handler threads share one PID. `static/app.js:19` declares `S.busy` and never uses it; `reprint()` at app.js:173 has no in-flight guard. A double-tap on a laggy phone is two concurrent POSTs for the same sale: same code, same PID, same path. Thread B's `stamp_code` truncates the file thread A is reading, and A's `finally: tmp.unlink()` deletes the file B is using.

Today the window is microseconds. Under the split it spans stamp → read → HTTP upload → render → write → settle, i.e. **seconds**. The split does not inherit this bug, it amplifies it by three orders of magnitude — and does so precisely because "no caller changes" is the selling point.

**Fix:** `tempfile.NamedTemporaryFile(prefix=f"mplabel_{code}_", suffix=".pdf", delete=False)`. Also wire up `S.busy` in `app.js` and disable the button while a print is in flight.

**A8 (serious). `print_lock` degrades to a warning in exactly the case it matters.**
`print_lock` (cli.py:464-481) derives its path from `cfg["home"]` with a `tempfile.gettempdir()` fallback, and a lock it cannot create is `log.warning("printing without a lock")` and carry on. `systemd/mplabel.service` sets `PrivateTmp=true` (confirmed), so the fallback gives each unit its own `/tmp` namespace — two units would silently stop interlocking and say so only at WARNING.

The proposed fix of moving it to `/run/lock/` is right about the path and **incomplete about permissions**. `/run/lock` is `1777`, so creation succeeds for anyone, but the file is owned by whoever prints first at whatever umask gives. The split creates at least two identities on the Pi: `printd` under its own unit, and a hand-run `mplabel selftest` at 11pm possibly under `sudo`. The second identity's `open(path,'w')` gets EACCES, hits the warn-and-continue branch, and two 124 KB single-burst writes interleave into firmware that discards bytes arriving while the head is moving.

**Fix:** path `/run/lock/mplabel-{basename(printer_device)}.lock`; create it `0660` owned `root:lp`, matching `udev/99-clabel-g4.rules` which already grants the `lp` group the device; use `os.open(..., O_CREAT|O_RDWR, 0o660)` and `os.fchmod` so the mode is right on first creation. **Inside `printd`, a lock that cannot be taken is a hard 503, not a warning** — the warn-and-continue branch exists so `probe`/`selftest`/`file` work with no data directory, and a daemon that has one gets no such excuse. Also wrap `printers.tspl_selftest`, which runs unlocked today at cli.py:1085-1093.

**A15 / A18 (serious). Print failures are invisible on the screen she is looking at.**
Three separate holes, all confirmed:
- `web._order_row` (web.py:229-244) returns no `notes` field. `h_pending` serialises with `_order_row`, so the Pending list cannot render a failure note. Only `_order_detail` (web.py:247-262) carries `notes`, and reaching it means tapping into a specific order she has no reason to suspect.
- `web._dispatch` (web.py:400-407) maps `ValueError` to a 400 with the message and **everything else to `self.fail(500, "internal error")`**. So tapping Print with the printer off shows her the literal string "internal error".
- "Mark shipped" (web.py:521) sets a status in `CLOSED_STATUSES`, which removes the row from `h_pending` and `cmd_pending` **with `printed_at` still NULL**. If a label did print but was recorded as failed, shipping the parcel permanently freezes the lie — and `allocate_code` then recycles that parcel's code to a live one, quietly undercutting the "two labels reading 7QF is obviously one duplicate" argument every design leans on.

**Fix:** add `notes` (or a derived `last_error`) to `_order_row`. Add a `PrintError(Exception)` mapped in `_dispatch` to a 502 carrying `str(exc)`. When marking shipped with `printed_at IS NULL`, either set `printed_at` to the ship time with a note, or require an explicit confirm — pick one, but do not leave the row able to close in a state the system believes impossible.

**A17 (fatal, partly). `cmd_pending` has no wrong-buyer backstop.**
`cmd_reprint` (cli.py:824) and `web._print_one` (web.py:592) both call `label_belongs_to`. `cmd_pending` does a bare `Path(r["label_pdf"]).exists()` then `log.error` + `continue` — confirmed at cli.py:893. `cmd_test_print` does not check either. That guard exists because a parcel was nearly posted to a stranger. Route both through `label_belongs_to`, and surface a refusal rather than a silent `continue`.

**A23 / A20 (annoying, becomes serious after a split). Nothing is pinned and version skew is undetectable.**
`requirements.txt` and `pyproject.toml` are all `>=`. `pyproject.toml` pins `version = "0.1.0"` and never moves — the entire reason `install_pi.sh:36` needs `--force-reinstall --no-deps`. So a `/healthz` "version" field would report `0.1.0` forever, through every skew, and `git describe` cannot help because `install_pi.sh:24` does `cp -r src pyproject.toml requirements.txt` with no `.git` at the destination (confirmed).

**Fix:** pin the four PDF deps in `requirements.txt`. Have `install_pi.sh` write `src/mplabel/_build.py` containing the source `git rev-parse --short HEAD` and a sha256 of `printers.py`. `/healthz` reports both; the `pi-http` client logs a warning on mismatch with its own. This is the only detector that catches the failure this repo has actually been burned by — running month-old code at an unchanged interface.

**Also in this group, cheap:** add `timeout=` to the two `subprocess.run` calls at printers.py:121 and printers.py:135 (both `check=True`, neither has one). Give `print_zpl` the `settle`/`media`/`gap_in` parameters the other raw backends have — it is the only one that silently drops `settle_seconds`, and freezing that into a wire contract would be worse.

### Group B — holes the split would open. Fix as part of it.

**C1 (was A3/A21). A wedged printer must not blind the health check.**
Design 1 argued single-threaded `HTTPServer` *is* the serialisation. That is false in its own design — it also holds the flock — and it costs the diagnostic. `_write_raw` has no timeout and no `O_NONBLOCK`; usblp blocks on write when the printer is out of paper, paused, lid open, or its buffer is full. Those are the ordinary 11pm conditions, and a mis-set `gap_inches` (still ASSUMED) is a leading cause. A single-threaded server wedged on that write stops accepting connections, so `curl pi:9101/healthz` hangs — the triage command goes dark in exactly the case it exists to diagnose, and the phone cannot tell "Pi unplugged" from "label jam".

**Fix:** `ThreadingHTTPServer` with a module-level `threading.Lock` plus the flock. **The lock is the serialisation; the thread count is not.** `/healthz` never touches the device and never takes the lock — it reports `printing_since` from a timestamp set under the lock, so a wedge is *visible* rather than silent.

**C2 (was A4). The TCP backlog is a queue, and it prints unattended.**
Design 1 claims "no queue". `HTTPServer` inherits `request_queue_size = 5`, so while `printd` is busy the kernel completes handshakes for connections it has never `accept()`ed. The client's `connect()` succeeds, urllib writes the PDF into the socket buffer, then hits `printd_timeout` and reports ambiguity. She clears the jam; `printd` unwedges, accepts the backlogged socket, and **prints minutes later to a client that is long gone**, with the 200 going nowhere.

**Fix, and it also solves the clock problem.** The client sends `X-MPLabel-Deadline: <seconds of patience>`. `printd` records `time.monotonic()` at the moment it finishes reading the request, and immediately before taking the device it checks whether that budget has elapsed. If it has, respond `410` and do not print. This is **purely local monotonic time** — no wall clock, no synchronisation between hosts, no RTC. Set `request_queue_size = 1` as well.

**C3 (was A11). The duplicate story has one layer, not two.**
Design 1's job-id replay set is unreachable by construction: its own client "retries only on `URLError` where the connection was never established", and a connection that was never established cannot have delivered a job. TCP already dedupes below HTTP. And `ensure_code` making a duplicate byte-identical does not *prevent* a duplicate, it makes one legible. The only duplicate that actually occurs is the human retry after an ambiguous timeout, which is correctly permitted.

**Fix:** stop claiming two layers, and make the one that matters durable. Replace the in-memory set with an append-only journal at `/var/lib/mplabel-printd/done.jsonl` — `{job, ts, bytes, sha256}` appended after `_write_raw` returns — plus `GET /printed?since=`. This survives a `printd` restart, which an in-memory set does not, and it converts A2's irreducible read-timeout ambiguity from "go look at the printer" into a query. Add `mplabel reconcile`, which reads the journal and marks matching sales printed. Cap the journal at the last few thousand lines.

**C4 (was A9/A19). No timestamp window. The Pi has no RTC.**
No Raspberry Pi through the Pi 4 has a battery-backed clock, and Bookworm restores time from `/var/lib/systemd/timesync/clock` — the last shutdown — until timesyncd reaches a server. `systemd/mplabel.service` orders on `network-online.target`, not `time-sync.target`. So the first print after the power cut that is the whole reason this design optimises for "Pi off is normal" is the one most likely to run against a clock hours behind. A ±300s window turns that into `401 bad signature` on every job, while the order side signs perfectly valid requests — and "401" reads unmistakably as a wrong `printd_secret`, so the night gets spent regenerating a key that was never wrong.

**Fix:** the HMAC covers `job + body digest` only. No timestamp, no window. Staleness is handled by C2's monotonic deadline and replay by C3's durable journal — neither needs a synchronised clock. Add `After=time-sync.target` to `mplabel-printd.service` anyway, as belt and braces for the log timestamps.

**C5 (was A1 again, at the new seam). `printd` must catch `BaseException`.**
Even after A1's fix converts the device-missing case, `printers.send` can still raise `SystemExit` for an unknown backend (printers.py:365), and `socketserver` catches only `Exception` before a bare `except:` re-raise (verified). A daemon that dies on a printer fault, restarts, and dies again on the retry is worse than one that returns 503. Wrap the send in `except BaseException` in `printd` specifically, map `PrinterUnavailable` → 503, and let the process live.

**C6 (was A13). The batch print will hit Cloudflare's 100-second timeout.**
`h_print_pending` (web.py:610-630) loops `_print_one` synchronously inside one HTTP request, and Cloudflare's edge returns 524 at 100 seconds regardless of the origin. CLAUDE.md records the real load case: nine items at once. Nine × (render + `settle_seconds` 2.0 + a network hop) plausibly crosses it. She gets an error page; `batchPrint()`'s catch (app.js:181-193) does **not** clear `S.sel` and does **not** set a busy flag, leaving the screen primed with the same nine selected and a "Hold to print 9" button — and she has every reason to press it, because from her side it failed. `h_print_pending` prints whatever ids it is given, printed or not.

**Fix, cheapest first:** (i) have `h_print_pending` skip rows where `printed_at IS NOT NULL` unless `force`, making a re-fire idempotent — this alone defuses it; (ii) clear `S.sel` for succeeded ids in the catch path and wire up `S.busy`; (iii) return partial results by streaming a chunked response or capping the batch at six with a "print the rest" follow-up. Do (i) and (ii) now; (iii) only if it still bites.

**C7 (was A22). The Settings screen becomes a plausible lie.**
`cli.DEFAULTS` always supplies `printer_backend`/`device`/`dpi`/`darkness`/`gap_inches` whether or not the host has a printer, and `web.h_system` (web.py:496-514) reads them off its own cfg. So after she tunes `gap_inches` to 0.15 on the Pi — the whole of open-work item 1 — her phone confidently displays 0.12, with no staleness signal, during the exact week those numbers change daily.

Two more of the same shape: `mplabel selftest` is dispatched **above** `printers.send` (cli.py:1085-1093 calls `printers.tspl_selftest(cfg["printer_device"])` directly), so adding `pi-http` to `BACKENDS` does not route it. And `mplabel probe` runs before `load_config` entirely (cli.py:1077-1079), globbing `/dev/usb` on whatever host it runs on.

**Fix:** `h_system` proxies `printd`'s `/healthz` and renders *that*, with the fetch time shown. Route `selftest` through the backend when `printer_backend == pi-http` (`POST /selftest`). Have `probe` say plainly which host it is describing and add `--remote` to fetch `/healthz`.

**C8 (was A16). The lookback alarm cannot live in the poller.** Deferred — it only bites if the order side leaves the Pi. Recorded as the named precondition in Phase 5.

---

## Status (updated as phases land)

| Phase | State |
|---|---|
| 0 — hygiene | **Done.** One deliberate omission: `print_zpl` got `settle` but not `media`/`gap_in` — ZPL media commands are unverified on any hardware here and that backend has never printed. |
| 1 — Group A bug fixes | **Done.** The lock is `0666` in `/run/lock` rather than `0660 root:lp`: same goal, without needing root to create it, which a `pi`-user service does not have. |
| 2 — validate on hardware | **Blocked — needs the printer.** `mplabel status` and `docs/phase2-hardware-checklist.md` are the tooling. **This is the gate.** |
| 3 — split over loopback | **Code complete, not deployed.** Tested against a fake device only. |
| 4 — printd off loopback | **Prerequisites done** (C3, C7, installer). The move itself is a config change, still gated on phase 2. |
| 5 — order side to k3s | Not started, and not recommended until the phase-5 precondition exists. |

## 6. Phased migration

No flag day. Every phase leaves a working system, and each is independently reversible.

### Phase 0 — pure hygiene, no behaviour change
Extract `printers.backend_kwargs(cfg, backend)` verbatim from the inline dict at cli.py:484-509, with a test asserting it produces exactly the current dict for all five backends. Move `print_lock` from cli.py into `printers.py` so `printd` can take it without importing `label` (and thus pdfplumber/pypdf). Add `timeout=` to printers.py:121 and :135. Give `print_zpl` its missing `settle`/`media`/`gap_in`. Gate `probe`'s `sudo lpinfo -v` behind `--cups`. Pin the four PDF deps.
**Deploy** with `--force-reinstall --no-deps`, print one real label, confirm nothing changed.

### Phase 1 — the Group A bug fixes
`PrinterUnavailable` replacing `SystemExit` (A1). The `head_dots` guard in `render_bitmap` (A5). `NamedTemporaryFile` for the stamped copy (A12). `print_lock` moved to `/run/lock` with 0660 root:lp and made a hard error inside daemons (A8). `notes` on `_order_row`, `PrintError` → 502 in `_dispatch`, and the mark-shipped-with-NULL-printed_at hole (A15/A18). `label_belongs_to` in `cmd_pending` and `cmd_test_print` (A17). Idempotent `h_print_pending` + `S.busy` in app.js (C6). `_build.py` build stamp from `install_pi.sh` (A20).
Each with a regression test first, per the house rule. **None of this depends on splitting anything, and all of it is worth having if you stop reading here.**

### Phase 2 — finish open-work item 1, on the single-process Pi
Nothing has moved. Print real labels and answer the three open questions: does one job advance exactly one die-cut label; is the ink centred or creeping; do the barcodes scan. Tune `gap_inches`, `printer_darkness`, `printer_speed`. Move that row of CLAUDE.md's table from ASSUMED to VERIFIED.
Also here, standalone and off to one side: the **paper-out read experiment** (A2). Open `/dev/usb/lp0` `O_RDWR`, write `<ESC>!?`, `select()` with a 500 ms timeout, see whether a byte comes back with the roll out. Record the result either way.
**Do not proceed past this phase until item 1 is signed off.** This is the gate.

### Phase 3 — split over loopback. Nothing moves.
Add `src/mplabel/printd.py` and the `pi-http` backend. Install `mplabel-printd.service`. In `/etc/mplabel.conf`: `printer_backend = pi-http`, `printd_url = http://127.0.0.1:9101`, a generated `printd_secret`, `printd_bind = 127.0.0.1`.
Everything is still on the Pi and the phone app is unchanged, but HMAC, HTTP framing, the temp file, the deadline check, the done journal, the lock-as-hard-error, and the 400/409/410/503/500 paths are all now exercised on real orders. Print one label, then a batch of three, and confirm one job still advances exactly one label.
**Rollback: `printer_backend = tspl`, restart.** This phase is the whole point — it de-risks the split before the split.

### Phase 4 — move `printd` off loopback. Still nothing else moves.
`printd_bind = 0.0.0.0`, `printd_url = http://<pi-ip>:9101` from the workstation, and confirm a signed print works across the network. Now the boundary is real and the order side could live anywhere. **Stopping here permanently is a good outcome.** You have a print agent you can stop touching, CUPS support intact for a future printer, and every Group A bug fixed.
Note what this does *not* fix: the label PDF now crosses the LAN in cleartext, carrying a buyer's home address. The HMAC authenticates and integrity-checks; it does not encrypt. If that matters, route `printd` through the existing cloudflared tunnel for TLS end to end — one hostname, no new software.

### Phase 5 — optional, and only against a named precondition
**Precondition: an alarm that lives outside the order side.** A cluster outage longer than `lookback_days` drops mail silently, and no in-process warning can detect its own absence. Acceptable answers: a cron on the Pi that curls the order side's `/healthz` and prints a physical warning label if it has been down for hours; or a Cloudflare healthcheck; or a dead-man ping. Until one exists, do not move the poller.

Then, and only then:
1. **Relative label paths.** `sales.raw_pdf` and `sales.label_pdf` store absolute paths (cli.py:578-579), and four places resolve them as absolute: `label_belongs_to` (cli.py:427, inherited by `cmd_verify`), `cmd_pending` (cli.py:893), and `web.safe_label_path` (web.py:205-219). Add `cli.label_path(cfg, stored)` accepting both forms, route all four through it, and add a guarded one-time migration next to the `MIGRATIONS` loop rewriting only values under the configured `home`. **Take a database copy first — this is the step that could make every archived label unverifiable, and `label_belongs_to` is what stands between a reprint and a parcel posted to a stranger.** Gate on `mplabel verify` producing byte-identical output before and after.
2. **Neutralise the Pi's stale copy** before the pod starts. Move `~/marketplace` to `~/marketplace.pre-split` and point the Pi's `home` at a directory that does not exist, so a reflexive `mplabel pending` on the Pi finds nothing rather than reprinting shipped parcels with recycled codes.
3. Cut over: stop `mplabel.service`, copy the data, single replica, `Recreate` strategy, RWO PVC on `zfs-general` or `local-path` — **never `nfs-media`**, because `connect_db` sets `journal_mode=WAL` and WAL needs a real `-shm` mapping and working POSIX locks. `Recreate` not `RollingUpdate`, because two pods would both run `listings.refresh()`, which executes `DROP VIEW`/`CREATE VIEW` DDL on a plain `GET /api/stats` (web.py:480-482).
4. Set `lookback_days = 30`, and put the config in **one** place — if the pod is env-configured, make it env-only and delete the mounted file, so nobody edits a file that silently loses to `MPLABEL_*` (cli.py:163-167).

---

## 7. File-by-file

**`src/mplabel/printers.py`** — `PrinterUnavailable` exception; `_write_raw` raises it instead of `SystemExit`; `head_dots` guard in `render_bitmap`; `print_lock` moved in from `cli.py` with the `/run/lock` path and 0660 root:lp; `backend_kwargs(cfg, backend)` extracted; `print_zpl` gains `settle`/`media`/`gap_in`; `timeout=` on both `subprocess.run`; `probe`'s `lpinfo` gated behind `--cups`; new `print_pi_http()` added to `BACKENDS` (~50 lines of `urllib.request`, per the urllib-not-requests rule). `build_tspl` and `_write_raw`'s write loop are otherwise **byte-for-byte untouched**.

**`src/mplabel/printd.py`** — new, ~200 lines. `ThreadingHTTPServer` + `threading.Lock` + the flock; HMAC verify; protocol check; monotonic deadline; 4x6 page assertion; `NamedTemporaryFile`; `printers.send(path, backend, **printers.backend_kwargs(cfg))` unchanged; `except BaseException` around it; done journal append; `/healthz`, `/printed`, `/selftest`. Imports only `printers` and stdlib — safe because `printers.py`'s top-level imports are stdlib-only and pypdfium2/PIL are function-local (verified at printers.py:41-42, 70-71).

**`src/mplabel/cli.py`** — `print_label` keeps its signature and all five call sites; only the temp-file naming changes and it delegates kwargs to `backend_kwargs`. `cmd_pending` and `cmd_test_print` gain `label_belongs_to`. `selftest` routes through the backend when it is `pi-http`. `DEFAULTS` gains `printer_head_dots`, `printd_url`, `printd_secret`, `printd_bind`, `printd_port`, `printd_timeout`. New `cmd_reconcile`. `main()` catches `PrinterUnavailable` → `SystemExit` so CLI output is unchanged.

**`src/mplabel/web.py`** — `_order_row` gains `notes`; `_dispatch` gains a `PrintError` → 502 branch; `h_print_pending` skips already-printed rows unless forced; `h_system` proxies `printd`'s `/healthz`; mark-shipped refuses or annotates when `printed_at IS NULL`.

**`src/mplabel/static/app.js`** — wire up the declared-but-unused `S.busy` (app.js:19); guard `reprint()` (app.js:173); clear succeeded ids from `S.sel` in `batchPrint()`'s catch (app.js:181-193).

**`systemd/mplabel-printd.service`** — new. `After=time-sync.target`. `ReadWritePaths=/var/lib/mplabel-printd /run/lock`. No `EnvironmentFile` for the IMAP password. Note the repo ships exactly one unit today and is already one short — `cli.py` defines `serve` with no unit for it; add that too.

**`install_pi.sh`** — writes `src/mplabel/_build.py` with the git hash and a `printers.py` content hash; installs the new unit; creates `/var/lib/mplabel-printd`.

### `cli.print_lock` — kept, moved, hardened
It does **not** dissolve under the split, and the reasoning that added it this session still holds. On the Pi it now serialises an inbound `printd` job against a local `mplabel selftest` or `file --print`, which run entirely unlocked today (cli.py:1085-1093). It moves to `printers.py` (so `printd` can take it without importing `label`), gets a device-derived path with explicit 0660 root:lp, and inside `printd` a lock that cannot be taken is a hard 503. The warn-and-continue branch survives **only** for the CLI paths it was written for.

### WAL and `busy_timeout` — kept, unchanged
Also do not dissolve. After Phase 4 the order side still has two writers on one file — the poll loop and the `ThreadingHTTPServer` web app, plus any hand-run `mplabel` CLI. After Phase 5 the pod is one process but still multi-threaded with per-thread connections (web.py:672-690). `synchronous=NORMAL` under WAL stays the right call for an SD card. The only new rule is the PVC storage class: WAL needs a real `-shm` file, so `zfs-general` or `local-path`, never NFS.

---

## 8. Verified vs assumed

In CLAUDE.md's style. Add these rows when the work lands; do not promote any of them on a passing test, because the tests use synthetic fixtures.

| Claim | Status |
|---|---|
| `SystemExit` from `_write_raw` escapes every `except Exception` in this codebase | **Verified here** by reading the source and confirming `issubclass(SystemExit, Exception)` is False, plus `socketserver`'s `except Exception` / bare `except:` structure. Not yet observed in production, but it is mechanical. |
| `_order_row` omits `notes`; `_dispatch` returns "internal error" for non-ValueError | **Verified** by reading web.py:229-244 and web.py:400-407. |
| `cmd_pending` never calls `label_belongs_to` | **Verified** at cli.py:893. |
| `render_bitmap` has no print-head width check | **Verified** — no such comparison exists anywhere in printers.py. |
| `probe` runs `sudo lpinfo -v` unconditionally | **Verified** at printers.py:420-422. |
| `print_lock` falls back to a `PrivateTmp` namespace and only warns | **Verified** at cli.py:464-481 plus `PrivateTmp=true` in the unit. |
| `printd` HTTP round trip does not perturb the printed output | **ASSUMED.** Verify in Phase 3 by printing the same archived label through `tspl` and through `pi-http` and comparing the paper: same code, same position, same darkness, one die-cut advance. |
| Does the G4 answer a TSPL status query? | **UNKNOWN and worth an hour.** Test `<ESC>!?` and `~HS` on `O_RDWR` with a `select()` timeout, with the roll out and the lid open. If it answers, A2 becomes recoverable. If it does not, record that and accept at-least-once. |
| A wedged `os.write` leaves `/healthz` answering | **ASSUMED.** Verify by holding the lid open mid-job and curling `/healthz` from another host. This is the whole justification for threading `printd`. |
| The deadline check actually prevents a backlogged late print | **ASSUMED.** Verify by wedging the printer, firing two prints, clearing the wedge, and confirming the second returns 410 and no second label emerges. |
| The Pi is reachable inbound from the k3s node network | **UNVERIFIED.** `raspberrypi`/`pi.local` did not resolve from this workstation. Phase 4 is the test; the whole push design depends on it. If it fails, the answer is a stdlib long-poll, not a broker. |
| `mplabel:///dev/usb/lp0` would keep usblp bound | **ASSUMED and not being relied on.** This is the CUPS-driver claim; this plan does not build on it. |
| Label paths are portable across hosts | **False today.** Absolute at cli.py:578-579 with four absolute resolvers. Phase 5 step 1. |

---

## 9. What I am not recommending, and why

- **Not NATS or any broker on the print path.** §4. The durable queue already exists in SQLite and a second one that can replay a job for a cancelled order is a regression. A bus for notifications only is a defensible future option.
- **Not a CUPS driver, filter, PPD or custom backend for the G4.** §3. It puts an unexecuted path in front of the only hardware-verified one, makes "printed" a text-scrape of `lpstat`, and adds a queue that can be left disabled needing `cupsenable` typed over ssh. The `cups-pdf` and `cups-raster` backends keep working unchanged for a future printer, which is the real content of "support CUPS".
- **Not IPP as the transport, hand-rolled or via `pycups`.** `lp` exit 0 means queued, and encoding IPP attributes by hand on the path where a wrong byte means a parcel does not ship is exactly the cleverness CLAUDE.md warns about.
- **Not shipping pre-rendered TSPL bytes over the wire.** It would move `dpi`/`darkness`/`gap` off the machine being tuned during the exact week they are being tuned, break the CUPS backends, and require changing `printers.py`.
- **Not Design 2's profile-hello / `Mp-Profile` hash mechanism.** It exists only to compensate for rendering having moved off the Pi. Keeping rendering on the Pi eliminates the entire class of bug those guards defend against. Recording this explicitly so nobody re-adds it later.
- **Not the 4.00x6.00in-only geometry assertion as *the* guard.** §5 A5. It checks a property already locked by `test_output_is_exactly_4x6` and passes the 300-dpi case that actually ejects blank labels. Do it, but do the `head_dots` check as well and understand which one is load-bearing.
- **Not a wall-clock timestamp window on the HMAC.** §5 C4. The Pi has no RTC and the failure presents as "401 bad signature", which reads as a wrong secret.
- **Not a single-threaded `printd`.** §5 C1. The lock is the serialisation; single-threading only buys a health check that dies when you need it.
- **Not moving the order side to k3s yet.** §1. It buys credential blast-radius reduction and an off-SD-card database, and it costs a silent mail-drop mode with no alarm that can detect its own absence, a stale-database wrong-parcel hazard on rollback, and a config split-brain. Revisit against the Phase 5 precondition.
- **Not deleting `print_lock` or the WAL settings.** §7. Both were added this session for reasons that survive the split.
- **Not touching the print path before open-work item 1 is signed off.** Phase 2 is the gate. Debugging unvalidated stock geometry with a network in the picture confounds two variables on the one part of this system currently known-good.