# Notifications

Three things earn one, and the design is blunt about it: **a parcel is
due, a label never printed, or money has no home.** Nothing else. A
notification that is not one of those trains her to swipe them all away,
and the one that matters is then the one she swipes away fastest.

The decision is the server's. The phone registers and nothing more —
two places deciding when to interrupt her is two places to get it wrong,
and the Pi is the one that knows what is due.

## What you have to do once

Push cannot be finished from this side. It needs three things out of the
Apple Developer account, none of which is in the repo:

1. **An APNs auth key** — Keys → new key with "Apple Push Notifications
   service" ticked. It downloads once as `AuthKey_XXXXXXXXXX.p8`. Put it
   on the Pi somewhere only the service user can read (`chmod 600`), and
   never in this repo — `.gitignore` covers `*.p8`.
2. **The Key ID** (the `XXXXXXXXXX` in that filename) and your **Team
   ID** (Membership → Team ID).
3. **The Push Notifications capability on the App ID.** Xcode's Signing
   pane adds it when the entitlement is present. Without it a *device*
   build fails at signing while the simulator carries on working, which
   is a confusing way to find out.

Then, in `/etc/mplabel.conf`:

```ini
[mplabel]
apns_key_path = /etc/mplabel/AuthKey_XXXXXXXXXX.p8
apns_key_id = XXXXXXXXXX
apns_team_id = YYYYYYYYYY
apns_topic = com.marchvector.Sellomatic
apns_environment = sandbox     ; production once the app is not a dev build
```

`apns_environment` matters more than it looks. A build signed with a
development profile gets a **sandbox** token, which the production host
rejects with `BadDeviceToken` — an error that reads like a malformed
token rather than one addressed to the wrong Apple.

## Running it

```bash
mplabel notify --dry-run     # the whole decision, printed, sends nothing
mplabel notify               # says each thing once
```

`--dry-run` is the one to reach for first: run it twice and it says the
same thing, because it does not record what it did not send.

An unconfigured install exits **78** (`EX_CONFIG`), the same refusal
`printd` makes for a missing secret — so a timer does not retry a
permanent error for ever.

A timer rather than the poll loop, because the useful times to be told
are not the times mail arrives. `install_pi.sh` writes both units -
installed and **not enabled**, like `printd` and the web app, because an
unconfigured `notify` refuses and a timer firing a permanent error twice
a day is a permanent error in the journal twice a day:

```bash
sudo systemctl enable --now mplabel-notify.timer
systemctl list-timers mplabel-notify.timer
```

It fires at 09:00 and 17:00 - over breakfast when something is due
today, and after work when a label never came out and there is still an
evening to fix it. `Persistent=true`, because a parcel that was due
while the Pi was off is still due.

## When it will not send

```bash
mplabel notify --check      # config, the JWT, curl, the devices. Sends nothing.
mplabel notify --test       # one deliberate notification, and what Apple said
```

`--check` exists because a refusal cannot tell you whose fault it is.
APNs answers a request it cannot classify with
`{"reason":"InternalServerError"}` and says no more, so the first
question is whether *our* request is well formed: is the key where the
config says, is the signature 64 bytes, does this curl speak HTTP/2, is
the token 64 hex characters, and was it registered against the
environment being sent to.

What the refusals usually mean:

| reason | usually |
|---|---|
| `BadDeviceToken` | `apns_environment` does not match the build the token came from - a development build gives a **sandbox** token |
| `InvalidProviderToken` | `apns_key_id` or `apns_team_id` does not match the `.p8` |
| `TopicDisallowed` | `apns_topic` is not the app's bundle id, or the key has no push capability |
| `InternalServerError` | Apple's, usually - it is retried once automatically, and `--test` prints `--check` after a second one. An unclassifiable request looks the same, so the first thing to rule out is **the wrong .p8**: an App Store Connect API key is also `AuthKey_*.p8` and is indistinguishable by eye from an APNs auth key. APNs needs the one made under **Keys** with the Apple Push Notifications service ticked |
| `ExpiredProviderToken` | the Pi's clock is wrong |

## Why curl and openssl instead of libraries

APNs wants HTTP/2 and an ES256-signed JWT. The stdlib has neither, and
the obvious answer — `httpx[http2]` plus `cryptography` — is a compiler
toolchain and about 40MB on a Pi that runs a deliberately short
dependency list. Both programs are already on the machine and already
relied on by this project's own instructions.

The cost is real and worth stating: the failure modes are a
subprocess's rather than a library's, so every call checks its exit
status and keeps stderr. `openssl` emits a DER signature where JOSE
wants raw `r||s`, so `notify._der_to_jose` unpacks it — twenty lines,
unit-tested against a signature openssl actually produced, because the
error Apple gives for a malformed one is `403 InvalidProviderToken` and
says nothing else.

## What is deliberately not here

- **No retry queue and no delivery guarantee.** APNs is best effort;
  late is worse than useless and twice is noise. Everything notified
  about is visible in the app, which is the source of truth — the push
  is a tap on the shoulder, not a channel.
- **No local notifications on the phone.** The phone does not know what
  is due without asking, and a second scheduler is a second thing to be
  wrong.
- **No "a print failed".** The printer is write-only, so nothing can
  know that. What is reported is "recorded and never printed", which is
  the set `mplabel pending` shows and the reason that command exists.
