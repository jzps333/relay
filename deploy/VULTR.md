# Cloud mode: run Relay's brain on Vultr

Normally everything runs on your Mac and the iPhone reaches it through ngrok.
In **cloud mode** the brain (AI calls, skills, workflows, ElevenLabs voice) runs on
a small Vultr server, and a little **device helper** on your Mac connects out to it
and does the tapping on the emulator and the Mac.

```
iPhone Shortcut --https--> Vultr server (Caddy -> Relay, CLOUD_MODE=1)
                                    ^
                                    | websocket (the Mac connects OUT, no tunnel needed)
                                    |
                     Mac: python -m relay.device_helper --> Android emulator / the Mac
```

Cloud mode is **opt-in**. Without `CLOUD_MODE=1`, Relay works exactly as before.

Throughout this guide, replace `1.2.3.4` with your server's real IP address and
`1-2-3-4.sslip.io` with the same IP written with dashes.

---

## 1. Create the server

1. Make a Vultr account. For hackathon credits, use the link on the MLH
   "Best Use of Vultr" prize page for your event (the amount and how to claim it
   are listed there).
2. In the Vultr dashboard: **Deploy +** -> **Deploy New Server**.
   - Type: **Cloud Compute (Shared CPU)**, the cheapest plan is enough
     (Relay only makes API calls; the heavy lifting happens at Claude/Gemini/ElevenLabs).
   - Location: the one closest to you (less delay per tap).
   - Image: **Ubuntu 24.04 LTS x64**.
   - SSH key: add your Mac's public key if you have one (`cat ~/.ssh/id_ed25519.pub`),
     otherwise Vultr shows a root password on the server's page.
3. Click **Deploy** and wait until it says *Running*. Copy its **IP address**.

## 2. Log in

On the Mac, in Terminal:

```bash
ssh root@1.2.3.4
```

(Type `yes` the first time. Use the root password from the Vultr page if you didn't add a key.)

## 3. Install the basics

```bash
apt update && apt -y upgrade
apt -y install python3-venv python3-pip git ufw
adduser --system --group --home /opt/relay relay     # a user just for Relay (not root)
```

## 4. Get the code

The repo is private, so the server needs permission to read it. Easiest: a
**fine-grained GitHub token** that can only read this one repo.

1. GitHub -> your picture -> **Settings** -> **Developer settings** ->
   **Personal access tokens** -> **Fine-grained tokens** -> **Generate new token**.
2. Repository access: **Only select repositories** -> `relay`.
   Permissions: **Contents: Read-only**. Expiration: a week is plenty.
3. Copy the token (starts with `github_pat_`), then on the server:

```bash
cd /opt
git clone https://github.com/jzps333/relay.git relay-src     # username: your GitHub name, password: the token
cp -a relay-src/. /opt/relay/ && rm -rf relay-src
cd /opt/relay
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

(Alternative: a read-only **deploy key**. `ssh-keygen -t ed25519` on the server, paste
`~/.ssh/id_ed25519.pub` into the repo's **Settings -> Deploy keys**, then clone with
`git@github.com:jzps333/relay.git`.)

The phone/Mac libraries in requirements.txt install fine on Linux; the server just
never uses them in cloud mode. If one of them fails to install, it is safe to remove
that line on the server.

## 5. Settings (`/opt/relay/.env`)

```bash
cp .env.example .env
nano .env
```

Fill in at least:

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
ELEVENLABS_API_KEY=...
JEV_API_KEY=...                 # if you use it
RELAY_TOKEN=<a long random value>
CLOUD_MODE=1
EMULATOR_PIN=...                # if you use voice unlock (the server sends it to the Mac helper)
RELAY_PASSPHRASE=...
```

Make a strong token (this server is on the public internet, so `change-me` is not OK):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save with Ctrl+O, Enter, Ctrl+X. Then lock the file and give it to the relay user:

```bash
chown -R relay:relay /opt/relay
chmod 600 /opt/relay/.env
```

## 6. Keep it running (systemd)

```bash
cp /opt/relay/deploy/relay.service /etc/systemd/system/relay.service
systemctl daemon-reload
systemctl enable --now relay
systemctl status relay          # should say "active (running)"
curl http://localhost:8000/health
```

`/health` should show `"device_helper_connected": false` (the Mac isn't connected yet).
Logs: `journalctl -u relay -f` (Ctrl+C to stop watching).

## 7. Open the firewall

Relay itself only listens on localhost; Caddy answers on ports 80 (for getting the
certificate) and 443 (HTTPS).

On the server (ufw):

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable                      # answer y
```

In the Vultr dashboard (if you use a Vultr Firewall group): **Network -> Firewall ->
Add Firewall Group**, add rules accepting TCP **22**, **80** and **443**, then attach
the group to your server. Do **not** open port 8000.

## 8. HTTPS with Caddy and a free sslip.io name

iOS Shortcuts and the helper's secure websocket (`wss://`) want a real HTTPS
certificate, which needs a hostname, not a bare IP. **sslip.io** gives you one for
free: `1-2-3-4.sslip.io` always points at `1.2.3.4`. (`nip.io` works the same way.)

Install Caddy (official instructions, copy-paste):

```bash
apt -y install debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt -y install caddy
```

Use the Caddyfile from the repo, with your hostname:

```bash
cp /opt/relay/deploy/Caddyfile /etc/caddy/Caddyfile
sed -i 's/1-2-3-4.sslip.io/YOUR-IP-WITH-DASHES.sslip.io/' /etc/caddy/Caddyfile
systemctl reload caddy
```

Test from the iPhone or Mac browser: `https://1-2-3-4.sslip.io/health` should show
`"ok": true` with a padlock. The first visit can take ~30 seconds while Caddy gets
the certificate. If it fails, check `journalctl -u caddy -f`; usually port 80 or 443
isn't open yet (step 7).

## 9. Connect the Mac (device helper)

On the Mac, in the Relay folder, with the emulator running:

```bash
pip install -r requirements.txt        # adds the `websockets` package
```

Make sure the Mac's `.env` has the **same `RELAY_TOKEN`** as the server. Then:

```bash
python -m relay.device_helper --server https://1-2-3-4.sslip.io
```

You should see:

```
[helper 10:15:02] connecting to wss://1-2-3-4.sslip.io/device/ws ...
[helper 10:15:03] connected to wss://1-2-3-4.sslip.io/device/ws (controlling: phone, mac)
[helper 10:15:03] server accepted this helper. Ready for commands.
```

Leave that window open. If the Wi-Fi drops or the server restarts, the helper
reconnects by itself. (Tip: put `RELAY_SERVER=https://1-2-3-4.sslip.io` in the Mac's
`.env` and just run `python -m relay.device_helper`.) Only offer one device with
`--devices phone`.

`https://1-2-3-4.sslip.io/health` now shows `"device_helper_connected": true`.

Location for "navigate to ..." still comes from the Mac: the helper reads the Mac's
location and moves the emulator's GPS, like in local mode.

## 10. Point the iPhone Shortcut at the server

In the Shortcut's **Get Contents of URL** actions, change the URL from the ngrok
address to:

```
https://1-2-3-4.sslip.io/command
```

(and `/reply` the same way). The `Authorization: Bearer <RELAY_TOKEN>` header stays,
using the token from the server's `.env`. No ngrok needed any more.

Live dashboard for the projector: `https://1-2-3-4.sslip.io/dashboard?token=<RELAY_TOKEN>`.

## Updating the server after a code change

```bash
ssh root@1.2.3.4
cd /opt/relay && sudo -u relay git pull          # if you cloned with a deploy key
# (or copy the changed files over with scp)
sudo -u relay .venv/bin/pip install -r requirements.txt
systemctl restart relay
```

## Switching back to local mode

Nothing in local mode changed, so going back is just:

1. On the Mac: stop the helper (Ctrl+C) and run the normal server again:
   `python -m relay.server --tunnel` (make sure the Mac's `.env` does **not** say `CLOUD_MODE=1`).
2. Change the Shortcut URL back to the ngrok address.
3. Optional: on the Vultr server, `systemctl stop relay` (or destroy the server in
   the Vultr dashboard so it stops costing credits).

## If something goes wrong

| You see | Fix |
|---|---|
| Relay says "something went wrong" and the server log says `No device helper connected` | Start `python -m relay.device_helper` on the Mac. |
| Helper: `the server refused the token (HTTP 403)` | `RELAY_TOKEN` differs between the Mac's and the server's `.env`. |
| Helper: `HTTP 404` | The server runs old code: update it (above) and `systemctl restart relay`. |
| Helper keeps retrying, no HTTP code | Wrong hostname, Caddy not running, or ports 80/443 closed. Test `/health` in a browser. |
| Steps fail with `didn't answer ... within 30 seconds` | The emulator is frozen or the Mac is asleep. Raise `REMOTE_TIMEOUT` in the server's `.env` if the Mac is just slow. |
| Audio links point at `http://localhost` | The unit must start uvicorn with `--proxy-headers` (it does in `deploy/relay.service`). |
