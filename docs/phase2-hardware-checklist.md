# Phase 2: validating the print path on real hardware

This is the gate. Nothing downstream — including the `printd` split, which
is written and tested but never deployed — should go live until this is
signed off. Changing the transport while the geometry is unmeasured means
a bad print you cannot attribute to either change.

It is also the part no amount of code can do. It needs the printer, the
roll of stock actually in the room, and somebody looking at the paper.

Run it on the Pi with `printer_backend = tspl` — the single-process
configuration that is running today. Budget about a dozen labels.

---

## 0. What is running

```bash
/opt/mplabel/venv/bin/python -m mplabel probe
curl -s localhost:8080/healthz          # if the phone app is up
```

`probe` no longer runs `lpinfo -v` unless you pass `--cups`: CUPS
discovery can claim the printer and unbind usblp, which is the fault the
command exists to diagnose. `/healthz` now carries a build stamp, so you
can tell whether the Pi is running the code you think it is — the version
in `pyproject.toml` never moves, so it could never tell you that before.

## 1. Does the printer talk back? — the experiment

```bash
/opt/mplabel/venv/bin/python -m mplabel status
```

Non-destructive: it sends a TSPL status query and waits half a second.
Nothing prints.

This is the highest-value unknown in the system. A successful write does
**not** mean a label came out — out of paper, head open, a jam and a wrong
gap all accept the bytes and print nothing, after which the row is marked
printed and *leaves* the Pending queue. The one failure that loses a
parcel is the one that hides it.

- **If it answers**, `printd` can refuse a job when the paper is out and
  the parcel stays visible. Record it as VERIFIED in CLAUDE.md.
- **If it does not**, printing stays at-least-once and the paper is the
  only source of truth. That is a finding worth writing down, not a gap
  to engineer around.

Try it twice: once normally, once **with the roll removed**. A reply that
does not change when the paper is out is not useful.

## 2. One job, one label

```bash
/opt/mplabel/venv/bin/python -m mplabel test-print
```

- Did exactly **one** die-cut label advance? A second, near-blank label
  means the geometry is wrong, not the language.
- Is the ink centred on the label, or has it crept up/down?
- Does the tracking barcode scan? Use a phone scanner app.
- Is the 3-character parcel code legible in the top right?

## 3. The batch case, which is the one that actually bites

She sold nine items at once and Gmail threaded them. Print three in a row:

```bash
/opt/mplabel/venv/bin/python -m mplabel pending --dry-run   # look first
/opt/mplabel/venv/bin/python -m mplabel pending
```

- Do **all three** come out, or does the second start creeping?
- Does the third land in the same place on its label as the first?

Creep across a batch is the classic wrong-`gap_inches` signature, and it
is invisible on a single label.

## 4. Tune, one variable at a time

In `/etc/mplabel.conf`, then `systemctl restart mplabel`:

| Key | Range | Symptom it fixes |
|---|---|---|
| `gap_inches` | try `0.10`–`0.15` | creep down the roll; printer hunting for a gap |
| `printer_darkness` | 0–15 | washed-out barcode, or bleed closing up the bars |
| `printer_speed` | 1–6 | smearing at speed |

`gap_inches = 0.12` is currently **ASSUMED** and has never been measured
against this stock. It is the most likely thing to be wrong.

If `GAP 0,0` (continuous) is ever set on die-cut stock the printer never
finds the label edge, and that is what creep looks like.

## 5. Write down what happened

Move these rows in CLAUDE.md's verified/assumed table:

- **TSPL gap value 0.12in** — ASSUMED → VERIFIED with the measured value,
  or corrected.
- **Printer status readback** — UNKNOWN → whatever step 1 showed.
- **Parcel code placement** — currently verified on the *rendered page*
  only, not on a thermal print. Step 2 settles it.

Then open-work item 1 is closed, and `printd` is unblocked.

---

## What is already built and waiting

- `mplabel status` — step 1.
- `mplabel printd` + `printer_backend = pi-http` — the split. Tested
  against a fake device only: a signed job crosses as a ~2KB PDF and is
  rendered to ~124KB of TSPL on the printd side. **Do not deploy until
  the above is signed off.**
- Rolling the split back is one line: `printer_backend = tspl`, restart.
