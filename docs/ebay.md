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
mplabel ebay skus            # SKUs already on the account; read-only
mplabel ebay setup           # the three policies and the location
mplabel ebay push <listing>  # an inventory item and an unpublished offer
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

## Publishing, and why it works on sandbox only

`ebay push <listing>` creates the inventory item and an **unpublished**
offer. `--publish` makes it live, and is **refused when
`ebay_environment = production`** - going live stays a decision made in
eBay's own UI where the whole listing is visible.

That gate is a property rather than a prompt, and it exists so the
publish path can be *exercised* at all: a design where publish cannot be
called means that code runs against her real account the first time
anybody tries it.

Even on sandbox, publishing refuses four things. Each is a failure eBay
would report worse than we can:

- **A suggested category.** `push` prints eBay's guesses from the title
  and will not publish until `--category` names one. A wrong category is
  a listing nobody searching for the thing will ever see.
- **Missing required aspects.** eBay refuses one per round trip, so they
  are read from the Taxonomy API first and named together.
- **No photographs.** eBay requires at least one, and fetches it itself.
- **A photograph it cannot fetch.** Checked locally before the publish
  call, because eBay's refusal for an unreachable image names the *field*
  rather than the reason - and on this deployment the likeliest reason is
  that the tunnel is down.

**The order of operations matters and is not tidiness.** The
`ebay_offers` row is written *before* the publish call, because
`/ebay/photo/<sha256>` serves a digest only when it is attached to a
listing with such a row. Written afterwards, the allowlist would 404
eBay's own image fetch and the publish would fail naming the image
field. The row is a precondition.

**Titles are cut to 80 characters and `push` says so.** Hers run past a
hundred, so this fires often; the cut falls at a word boundary where
there is one. The desk shows the full title and eBay shows the cut, and
nothing else would say they differ.

### Business Policies is a programme, and the account has to join it

**Correction to an earlier note here: the opt-in *can* be done from this
repo.** `POST /sell/account/v1/program/opt_in` exists, so `ebay setup`
checks first and can do it.

An account that has not joined `SELLING_POLICY_MANAGEMENT` cannot have
business policies at all, and eBay's refusal for that is **not** a
sentence about programmes. A real sandbox account answered the policy
list with:

```
20403: Invalid .
```

- its own message template with an empty field name, which reads as a
malformed request and sends you to look at the query string. `setup`
therefore asks `get_opted_in_programs` **first**, because that question
has a legible answer.

```bash
mplabel ebay setup            # says which programmes the account is in
mplabel ebay setup --opt-in   # joins, then stops
mplabel ebay setup            # again, once eBay has processed it
```

`--opt-in` is a flag rather than automatic for the same reason
`--publish` is gated: joining changes how *every* listing on the account
is managed, and that is not a side effect of a command someone ran to
find out what was wrong. eBay also says it can take **up to 24 hours**,
so a successful call is a request rather than a state change - `setup`
says so and stops, instead of going on to create policies that would be
refused for the next day.

### The shipping service is asked for, not written down

eBay's shipping vocabulary is **per-marketplace and it moves**.
`USPSGroundAdvantage` replaced First Class Package in 2023, is a real
service, and a real sandbox account refused it outright:

```
20403: Please select a valid shipping service.
       (XPATH=DomesticItemShippingService[0].shippingService)
```

So `setup` reads
`/sell/metadata/v1/shipping/marketplace/<id>/get_shipping_services` and
picks the best service **that marketplace actually offers**, in a
preference order. Two things it drops: anything eBay flags as not valid
for the selling flow (it lists them and will not take them on a policy),
and anything international - a domestic option carrying an international
service is refused, and that refusal names the field rather than the
mistake.

```bash
mplabel ebay setup --shipping-service list   # what this account can use
mplabel ebay setup --shipping-service USPSParcel
```

**`USPSParcel` is USPS Ground Advantage.** USPS renamed the service in
2023; eBay updated the *description* and kept the legacy code. So
`USPSGroundAdvantage` - the name of the thing - matches nothing at all,
which is what the first attempt sent. The preference list puts
`USPSParcel` first because that is what a small parcel actually goes by,
and falls back to matching the **description** when no code does, since
the description is the half that tracks what a service is called.

The real account offered eighty-odd services and put
`validForSellingFlow` on **none** of them, so treating an absent flag as
usable is load bearing: dropping the unflagged would have emptied the
list and refused on an account offering eighty.

### Where parcels are posted from

The inventory location needs a real address - the postcode, or the city
and state. Sending only the country is `25802: Input error`, which names
no field.

```ini
ebay_location_postcode = 46176
```

`setup` refuses before the call rather than defaulting, and the reason
is not the API: **eBay shows buyers a delivery estimate computed from
this address**, so a placeholder would be a wrong promise on every
listing rather than a tidy default.

If the metadata call itself fails, `setup` falls back to the first
preference and **says it did**. Asking is better than assuming, stopping
is worse than both, and saying which happened is what makes a later
refusal legible.

### `ebay setup` needs a re-auth

It creates the policies, so it needs the `sell.account` **write** scope.
`refresh_access` replays the scopes that were actually granted -
deliberately, so widening `SCOPES` never silently widens a token you
already consented to - which means a token minted before this existed
cannot be refreshed into working. `setup` says so and exits 78 rather
than letting eBay answer 403, which reads as the account lacking a
permission rather than the token lacking a scope.

```bash
mplabel ebay auth            # again, to grant sell.account
mplabel ebay setup --dry-run # what it would create
mplabel ebay setup           # then paste the ids it prints into the config
```

It will **not** rewrite a policy that already exists. A policy is how
she actually ships and returns; a tool that overwrites one because its
own defaults differ is a tool that changed her terms without being
asked.

## The one public route, and what it will not serve

eBay fetches listing photographs itself: the Sell REST APIs have no image
upload, and `imageUrls` must be a public **https** URL. Her photographs
sit behind bearer auth, so there is one unauthenticated route:

```
GET https://<tunnel>/ebay/photo/<sha256>
```

`ebay_photo_base` is the outside of the tunnel, without a path. It is a
separate key from `ebay_notification_endpoint` on purpose - that one is a
whole URL eBay hashes into the deletion challenge, where a trailing slash
changes the meaning, and this one is a base that gets a path appended.

**What it refuses is the design.** A photograph is served only when its
digest is attached to a listing that has a row in `ebay_offers` - the
join *is* the allowlist. Everything else is 404: every receipt, every
untriaged capture, every photograph of something that was never listed. A
receipt carries what she paid and where she was, and it can never be
served here even by someone who learns its sha256.

Three properties worth not breaking:

- **The digest is matched in the route pattern** - 64 lowercase hex and
  nothing else - so no path can be traversed and no other shape reaches
  the handler at all.
- **Both refusals are byte-identical.** "No such photo" and "that photo
  exists but is not listed" must be indistinguishable, or the route is an
  oracle telling an anonymous caller which digests are in the database.
- **It takes no query parameters, and must not grow any.** The moment it
  does, it has stopped being a way to hand eBay some bytes and become a
  way to ask this server questions without a token.

The bytes themselves are already public - eBay shows them on the listing.
What is not public is everything else in `photos/`.

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
