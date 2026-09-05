# Getting the app onto her phone

Both clients need HTTPS, for different reasons and with no way round
either:

- **The PWA** needs a secure context or Safari refuses `getUserMedia`,
  so the scanner never opens. A home-screen install over plain `http`
  also behaves oddly enough to be worth avoiding.
- **The native app** ships with no ATS exception on purpose, so it will
  not talk to a plain-`http` address at all.

A LAN IP cannot have a real certificate, so the answer is a tunnel.

## The order this goes in

`mplabel serve` first, then the tunnel. A tunnel pointing at nothing is
a 502 that looks like a tunnel problem and is not.

```bash
cd ~/claber && git pull
sudo ./install_pi.sh                                    # writes the unit
/opt/mplabel/venv/bin/pip install --force-reinstall --no-deps ~/claber

/opt/mplabel/venv/bin/python -m mplabel passwd          # prompts, prints a line
sudo nano /etc/mplabel.conf                             # paste web_password_hash
sudo systemctl enable --now mplabel-web
curl -s localhost:8080/healthz                          # should answer
```

`install_pi.sh` is the step that is easy to skip. A `git pull` and a pip
install update the package and **never write a unit file** - which is
exactly how `mplabel-printd.service` did not exist last time, presenting
as `Failed to enable unit: ... does not exist`.

If the unit refuses, it exits 78 and **stays dead** rather than flapping,
with one legible line in the journal. That is deliberate.

## Today: a quick tunnel

Gets a working HTTPS URL in about a minute, with no account and no DNS.

```bash
sudo apt install -y cloudflared    # or the .deb from Cloudflare if apt has no package
cloudflared tunnel --url http://localhost:8080
```

It prints a `https://<random-words>.trycloudflare.com` URL. That is the
address to type into the app, and to open in Safari for **Share > Add to
Home Screen**.

**Two things to know before using this for anything but a test.**

It is **public**. Anyone with the URL reaches the login page, and behind
that password is a database of buyers' names and home addresses. The URL
is random and unlisted, which is not the same as private.

It is **ephemeral**. The hostname changes every time the command
restarts. The native app stores the address, so a new URL means retyping
it - and a home-screen install pinned to the old one simply stops
working.

Fine for this afternoon. Not what to leave running.

## Then: a named tunnel, with Access in front

This is what `web.py`'s own docstring assumes, and the reason its
password is described there as the *inner* layer:

> outer   Cloudflare Access in front of the hostname. Does the real work.
> inner   the password and signed cookie below, so the Pi is not naked if
>         the tunnel is misconfigured or someone is already on the LAN.

A named tunnel without Access in front is running with the inner layer
only. Set both up together.

```bash
cloudflared tunnel login                       # opens a browser, picks a zone
cloudflared tunnel create mplabel              # writes ~/.cloudflared/<uuid>.json
cloudflared tunnel route dns mplabel mplabel.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: mplabel
credentials-file: /home/pi/.cloudflared/<uuid>.json
ingress:
  - hostname: mplabel.example.com
    service: http://localhost:8080
  - service: http_status:404
```

Then let cloudflared install its own unit - it maintains that, and a
hand-written one here would duplicate something we do not own:

```bash
sudo cloudflared service install
sudo systemctl status cloudflared
```

Finally, in the Cloudflare dashboard: **Zero Trust > Access >
Applications**, add a self-hosted app for that hostname, policy = allow
your two email addresses. Until that exists the hostname is the quick
tunnel again, only permanent.

**The credentials file is a secret.** `~/.cloudflared/<uuid>.json`
authenticates as the tunnel; it belongs at mode 600 and never in this
repo. Same rule as `printd_secret`.

## What not to do

**Do not put the print path through the tunnel.** That decision is
already recorded: a mesh VPN or an ssh tunnel between the two machines,
because the tunnel puts an internet dependency on the path between two
machines in one house, in a system whose whole point is that a parcel
ships today. Cloudflare stays for the phone, where an outage costs a
page load rather than a parcel.

**Do not relax ATS in the iOS app to skip this.** The token and the
password cross the network in clear over `http`, and the exception has a
way of outliving the afternoon that justified it.
