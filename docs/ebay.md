# eBay

The other selling channel, and the first one with an API. `listings.py`
opens by explaining that Marketplace has none, so a catalogue has to be
assembled out of a mailbox; eBay is the opposite situation, and that is
the whole point.

This document is the eBay-side setup. Almost none of it can be done from
this repo, which is why it is written down.

## What is built so far

The plumbing, and a survey of the orders.

```bash
mplabel ebay auth            # prints the consent URL, then takes the code
mplabel ebay check           # config and tokens; changes nothing
mplabel ebay pull --dry-run  # the orders it would record; writes nothing
```

`pull` **refuses without `--dry-run`** and exits 2. Writing is the next
slice; a command that silently does less than its name says is worse
than one that stops.

Nothing writes to the database yet, and nothing has been sent to eBay in
anger. Every row this adds to CLAUDE.md's verified/assumed table is
**ASSUMED** - the order fixture is built from eBay's documented schema
and has never been compared with a real payload, so a green suite proves
the mapping is self-consistent and nothing more.

Three things in that mapping are worth knowing, because each is a trap
this repo has fallen into before in another form:

- **`price` is the line items, not `pricingSummary.total`,** which
  carries delivery and sales tax. The wrong field would put tax into
  `v_monthly.gross` and into the median `listings.worth` prices the next
  object off.
- **`ship_by` is converted to a local date.** eBay sends RFC 3339 UTC,
  and `notify.due_parcels` compares this column as a *string* - so a raw
  stamp is never `<= '2026-09-15'` and a parcel due today would be
  reported the day after it was due.
- **`message_id` is `ebay:<orderId>`, never NULL.** Six call sites on
  the print path key on it and all six fail silently on a falsy one.

## What you have to do once

### 1. A developer account and a keyset

`developer.ebay.com` → Application Keys. There are **two keysets**,
sandbox and production, and they look alike. Take:

- **App ID**, also called Client ID → `ebay_app_id`
- **Cert ID**, also called Client Secret → `ebay_cert_id`

### 2. An RuName

On the same page, "User Tokens" → "Get a Token from eBay via Your
Application" → add a redirect URL, and eBay mints an **RuName** beside
it. That string is what goes in `ebay_ru_name`.

The RuName is not a URL. eBay resolves it to the redirect you configured;
putting the URL itself in `redirect_uri` is rejected as a mismatch, which
reads as the redirect being misconfigured rather than as the wrong kind of
value having been sent.

The redirect URL never has to work. Consent happens in a browser
somewhere else, eBay redirects with `?code=...` on the end, and that code
is copied out of the address bar by hand — the Pi is headless and does not
listen for a callback. A URL that 404s is fine; the code is still in the
address bar.

### 3. Then, in `/etc/mplabel.conf`

```ini
[mplabel]
ebay_environment = sandbox
ebay_app_id      = YourApp-something-SBX-abc123-def456
ebay_cert_id     = SBX-abc123def456-7890-1234-5678-9abc
ebay_ru_name     = Your_Name-YourApp-someth-abcdefgh
```

`ebay_cert_id` is a secret and `mplabel config` redacts it, so
`MPLABEL_EBAY_CERT_ID` in the environment is the better place for it than
a file on disk.

### 4. Consent

```bash
mplabel ebay auth                       # prints a URL
# open it elsewhere, sign in as the seller, agree
mplabel ebay auth --code 'v^1.1#i^1#...'
mplabel ebay check
```

The code is url-encoded in the address bar and expires in a few minutes.
Quote it — it contains `#`, which a shell otherwise treats as a comment
and silently truncates.

## Before anything reaches production

Two things gate the production account, and neither is optional.

### Business Policies, and three policies under it

The Inventory API cannot create an offer for a seller who is not opted in
to Business Policies. Opt in from My eBay, then create a **fulfillment**,
a **payment** and a **return** policy, plus an **inventory location**, and
put their ids in the config:

```ini
ebay_merchant_location  = home
ebay_fulfillment_policy = 6••••••••••0
ebay_payment_policy     = 6••••••••••0
ebay_return_policy      = 6••••••••••0
```

The trap: these read as publish-time requirements and they are not. eBay
validates them when the **offer is created**, so they have to exist before
the first push, even though this system deliberately never publishes.

### Marketplace account deletion

eBay requires every developer either to subscribe to marketplace
account-deletion notifications or to opt out on the grounds of storing no
eBay data. We will store buyers' names and shipping addresses, so **opting
out is not available to us**, and no production call can be made until
this is in place.

That means a public HTTPS endpoint that answers a challenge and accepts
signed POSTs. The Cloudflare tunnel already carries the phone app, so it
is a route on the same server — but it is the one part of this system that
has to be up when nothing else is, and the Pi behind a home tunnel is
exactly the machine that disappears when the router reboots.

Not built yet. `ebay_verification_token` and `ebay_notification_endpoint`
exist in the config for it.

## The eighteen-month fuse

The access token lasts two hours and refreshes silently. The **refresh**
token lasts about eighteen months and then everything stops with
`invalid_grant`.

eBay reports that lifetime **only in the reply to the initial
authorization-code exchange** — a refresh never repeats it — so the expiry
is recorded at that moment or it cannot be recovered afterwards.
`mplabel ebay check` counts the days down and starts complaining at
thirty. There is no second warning from eBay.

`sheets.py` refused OAuth for this exact reason and used a service account
instead. eBay has no service-account equivalent, so the fuse is the price
of the channel.

## When it will not work

```bash
mplabel ebay check     # exits 78 if anything needs attention
```

`check` exists for the same reason `notify --check` does: a refusal cannot
tell you whose fault it is. eBay is worse than most — an unscoped token, a
sandbox token sent to production, and a genuinely unauthorised account are
three variations on the same 401.

| what it says | usually |
|---|---|
| `invalid_client` | `ebay_app_id` or `ebay_cert_id` is from the other environment's keyset |
| `invalid_grant` on `auth` | the code expired, or was pasted with the `#` truncated by the shell |
| `invalid_grant` on a refresh | the eighteen months are up. Re-run `ebay auth` |
| `the stored token is for sandbox` | `ebay_environment` changed under a token minted for the other one |
| a 403 on every call | the refresh went out without its `scope` parameter, so the token carries none. Not a permission on the account |
| `redirect_uri` mismatch | a URL was sent where the RuName belongs |

An unconfigured install exits **78** (`EX_CONFIG`), the same refusal
`printd` and `notify` make, so a timer does not retry a permanent error
for ever.

## Why urllib here and curl in notify.py

`notify.py` shells out to curl and openssl, and that is not the house
style — it is a concession to APNs, which needs HTTP/2 and an ES256-signed
JWT the stdlib cannot produce. eBay needs neither: plain JSON over HTTPS
with a bearer token. So this follows the actual rule in CLAUDE.md and uses
`urllib`.

The exception will be the account-deletion endpoint, whose
`X-EBAY-SIGNATURE` is ECDSA and will have to go back to `openssl` — with
the DER-versus-`r||s` trap `notify._der_to_jose` already records.

## What is deliberately not here

- **No `ebay publish`.** Going live is a decision made in eBay's own UI,
  where the whole listing is visible. Adding the call later is one method;
  the absence is the safety property.
- **No scraping, same as Marketplace.** The reasoning in CLAUDE.md applies
  unchanged, and here there is an API, so there is not even a temptation.
- **No order-event subscriptions.** The poll loop already runs every two
  minutes. A subscription buys latency we do not need, and costs signature
  verification, a subscription lifecycle and a delivery path with no
  guarantee. `getTopics` is how to revisit that with data.
