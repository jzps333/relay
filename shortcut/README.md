# Building the "Relay" iPhone Shortcut

The Shortcut is the whole iPhone app. It listens to you, sends your words to the Mac, and speaks the answer.
You build it once in the **Shortcuts** app (about 10 minutes). Action names in **bold** are exactly what you
type into the search bar at the bottom of the shortcut editor.

## Easiest: a public link with Cloudflare (works with your VPN on, at hackathons, on cellular)

If the iPhone can't reach the Mac's Wi-Fi address (a VPN like Tailscale on the phone, or Wi-Fi that
blocks devices from seeing each other), use a tunnel instead:

1. On the Mac, once: `brew install cloudflared`
2. Start Relay with the tunnel: `python -m relay.server --tunnel`
3. It prints a **PUBLIC LINK** like `https://random-words.trycloudflare.com`. Test
   `https://random-words.trycloudflare.com/health` on the iPhone (any browser; it's https, so Brave and
   Safari are happy).
4. Use that link as the `Server` Text in step 3 of the Shortcut.

The free link changes each time you restart, so update the `Server` Text when it does. Every request
still needs your token, so strangers who find the link just get "401".

## 0. Before you start (on the Mac)

1. Put a long random `RELAY_TOKEN` in `.env`. To make one:
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Start the server with:
   ```bash
   python -m relay.server
   ```
   It prints something like:
   ```
   Put this URL in the iPhone Shortcut (Get Contents of URL):
       http://192.168.1.20:8000/command
   Test from the iPhone in Safari (must say "ok": true):
       http://192.168.1.20:8000/health
   ```
   Write down the `http://192.168.1.20:8000` part (yours will differ). That is your **server address**.
   If it shows a big WARNING about the token, fix `.env` first and restart.
3. The first time, macOS may pop up *"Do you want the application Python to accept incoming network
   connections?"* Click **Allow**. If you miss it, see Troubleshooting.

## 1. Test the connection first (30 seconds, saves a lot of pain)

1. Make sure the iPhone is on the **same Wi-Fi** as the Mac.
2. On the iPhone, open **Safari** and go to `http://<your server address>/health`,
   e.g. `http://192.168.1.20:8000/health`.
3. You should see `{"ok":true,"device_default":"phone","emulator_connected":true}`.
   - `emulator_connected: false` is OK for now; it only means the Android emulator isn't running yet.
   - If the page doesn't load, stop and go to **Troubleshooting**. The Shortcut won't work until this does.

## 2. Create the shortcut and name it

1. Open **Shortcuts** -> **+** (top right).
2. Tap the name at the top -> **Rename** -> type `Relay` -> **Done**.
   The name is what Siri listens for, so later you just say **"Hey Siri, Relay"**.

## 3. Save your address and token in variables (type them only once)

1. Add **Text**. Type your server address, e.g. `http://192.168.1.20:8000` (no `/` at the end).
2. Add **Set Variable**. Tap *Variable Name* -> type `Server`. (Its input is the Text above.)
3. Add **Text**. Type `Bearer `, **with one space after it**, then your token.
   Example: `Bearer pick-a-long-random-string`
4. Add **Set Variable**. Name it `Auth`.

## 4. Listen and send your words

1. Add **Dictate Text**. Tap the small arrow on it: Language = English, **Stop Listening = After Pause**.
2. Add **Get Contents of URL**.
   - Tap *URL* -> insert the `Server` variable -> type `/command` right after it
     (it should read `Server/command`).
   - Tap the arrow / **Show More**.
   - **Method**: `POST`.
   - **Headers**: tap **Add new header** -> Key `Authorization`, Value -> insert the `Auth` variable.
   - **Request Body**: `JSON`. Tap **Add new field** -> **Text** -> Key `text`,
     Value -> insert **Dictated Text**.
3. Add **Set Variable**. Name it `Response`. (Input: Contents of URL.)

The server answers with JSON like:
`{"say": "Opened Maps.", "audio_url": "http://192.168.1.20:8000/audio/ab12cd.mp3", "needs_reply": false, "session_id": "a1b2"}`
`say` is always plain text, ready for Speak Text. `audio_url` is empty unless you set an ElevenLabs key.

## 5. Speak the answer, and answer its questions

Relay sometimes asks "Send this to Mom?". A **Repeat** lets you answer and hear the next reply.

1. Add **Repeat**. Set it to **10** times. Put **every action in steps 2-9 inside the Repeat**
   (drag them between *Repeat* and *End Repeat*).
2. Add **Get Dictionary Value** -> *Get* **Value** *for* `say` *in* `Response`.
   Then add **Set Variable** -> name `Say`.
3. Add **Get Dictionary Value** -> **Value** for `audio_url` in `Response`.
4. Add **If**. Input: that Dictionary Value. Condition: **has any value**.
   - Inside **If**: add **Get Contents of URL**. URL = the Dictionary Value (`audio_url`).
     Show More: Method `GET`, header `Authorization` = `Auth`. Then add **Play Sound**
     (input: Contents of URL).
   - Inside **Otherwise**: add **Speak Text** with `Say`. (If you see **Wait Until Finished**, turn it on.)
     This is the built-in iPhone voice, used whenever there's no ElevenLabs audio.
   - **End If** is added for you.
5. Add **Get Dictionary Value** -> **Value** for `needs_reply` in `Response`.
6. Add **If**: that value **is** `1`. (Shortcuts shows JSON `true` as `1`. If your iOS offers
   **is true** / **Yes**, pick that instead.)
7. Inside this **If**:
   1. **Dictate Text** (Stop Listening = After Pause).
   2. **Get Dictionary Value** -> **Value** for `session_id` in `Response`.
   3. **Get Contents of URL**: URL = `Server` + `/reply`. Method `POST`, header `Authorization` = `Auth`.
      Request Body `JSON` with two Text fields:
      `session_id` = the session_id Dictionary Value, `text` = the new **Dictated Text**.
   4. **Set Variable** -> `Response` (input: this new Contents of URL). This replaces the old reply,
      so the next time around the Repeat speaks the new answer.
8. Inside **Otherwise**: add **Stop This Shortcut** (no question = we're finished).
9. **End If**, then **End Repeat** (both added for you).

Tip: if a long task says "...I'm still working on it", answer anything ("okay"). Any `/reply` on a
running task waits up to 25 more seconds and tells you the newest update.

## 6. Run it

1. Tap the play button at the bottom of the editor and say "what's on my screen?".
2. The first time, iOS asks **"Allow Relay to connect to 192.168.x.x?"** -> tap **Always Allow**.
   Do this once with the phone unlocked (with VoiceOver, or with someone sighted nearby).
3. Now try **"Hey Siri, Relay"**.

## 7. Hands-free options

- Settings -> Siri -> **Listen for "Hey Siri"** (or "Siri") on. With AirPods in, it works with the phone
  in your pocket.
- Settings -> Siri -> **Allow Siri When Locked** on.
- Settings -> Accessibility -> Touch -> **Back Tap** -> Double Tap -> **Relay**.
- iPhone 15 Pro and later: Settings -> **Action Button** -> Shortcut -> **Relay**.

To be honest about limits: from the lock screen, iOS may still say "unlock your iPhone to continue" for
some actions, especially the first run, the "Allow Relay to connect" prompt, and sometimes playing audio.
If that keeps happening, unset the ElevenLabs key (Speak Text is the most lock-screen-friendly), or unlock
with Face ID when asked.

## 8. Optional: "Relay Stop"

New shortcut named `Relay Stop`: **Get Contents of URL** -> `Server` + `/stop`, Method `POST`,
header `Authorization` = `Auth`. "Hey Siri, Relay Stop" cancels whatever is running.
(Saying "stop" as a normal Relay command works too.)

## Troubleshooting

| What you see | What it means / fix |
|---|---|
| `/health` won't load in Safari, or the Shortcut says "could not connect" / times out | 1) Phone and Mac on the **same Wi-Fi** (not guest Wi-Fi, not cellular). 2) The server is running (`python -m relay.server` in a Terminal you left open). 3) **Wrong IP**: the Mac's IP can change; restart the server and copy the address it prints, then fix the `Server` Text in the shortcut. 4) The macOS firewall, below. |
| You never clicked "Allow" for Python | System Settings -> Network -> **Firewall** -> Options. Find Python and set it to **Allow incoming connections** (or turn the firewall off to test). Then restart the server. |
| Error **401** / "bad or missing token" | Token mismatch. The `Auth` Text must be exactly `Bearer ` + the token in `.env` (one space, no quotes, no extra space at the end). The server prints the first 4 characters to compare. Restart the server after editing `.env`. |
| Error **404** on `/reply` ("unknown session_id") | The server restarted mid-conversation. Just start over with "Hey Siri, Relay". |
| `/health` works in Safari but the Shortcut fails | Check the URL is `Server/command` (no double `//`), Method is **POST**, Request Body is **JSON** with key `text` (lowercase). |
| It speaks "Done." / nothing useful, or you hear silence | Check the `say` key spelling. If ElevenLabs audio is silent, delete the ElevenLabs key in `.env` and it will use Speak Text. |
| `emulator_connected: false` | Start the emulator (Android Studio -> Device Manager -> play) and check `adb devices` lists it. |
| Works at home, not elsewhere | Expected on plain Wi-Fi. Use Tailscale (next section). |

## Tailscale: works when the phone is off the Mac's Wi-Fi

Tailscale makes a private network between your devices, so the phone reaches the Mac from cellular or any
Wi-Fi without opening ports on your router.

1. Mac: install Tailscale (tailscale.com/download or the Mac App Store) and sign in.
2. iPhone: install **Tailscale** from the App Store, sign in with the **same account**, turn the VPN on.
3. In the Tailscale admin page (login.tailscale.com -> Machines) find the Mac's name or `100.x.y.z` IP.
   Turn on **MagicDNS** (DNS tab) so you can use the name.
4. Change the `Server` Text (step 3) to `http://<mac-name>:8000` or `http://100.x.y.z:8000`.
   Test `/health` in Safari again. This also works at home, so you only need one shortcut.
5. Keep the Mac awake: System Settings -> Battery/Energy -> "Prevent automatic sleeping when the display
   is off", or run `caffeinate -dis` in a Terminal.

Keep `RELAY_TOKEN` long and random even on Tailscale: this server can tap, type and call on your behalf.
