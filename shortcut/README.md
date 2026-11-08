# Building the "hands-free mode" iPhone Shortcut

The Shortcut is the whole iPhone app. It listens to you, sends your words to the Mac, and speaks the
answer. You build it once in the **Shortcuts** app (about 10 minutes). Action names in **bold** are exactly
what you type into the search bar at the bottom of the shortcut editor.

Why the name **"hands-free mode"** and not "Relay": Siri hears "Relay" and opens the **Amazon Relay** app
instead. "Hey Siri, hands-free mode" is never confused with anything.

## 0. Before you start (on the Mac)

1. Put a long random `RELAY_TOKEN` in `.env`. To make one:
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Install a tunnel, so the iPhone can reach the Mac from anywhere (cellular, hackathon Wi-Fi, with a VPN on):
   - **ngrok (preferred, the link never changes)**: make a free account at ngrok.com, then
     `brew install ngrok` and `ngrok config add-authtoken <your ngrok token>`.
   - **Cloudflare (fallback, the link changes every restart)**: `brew install cloudflared`.
3. Start the server with the tunnel:
   ```bash
   python -m relay.server --tunnel          # ngrok if installed, else Cloudflare
   python -m relay.server --tunnel ngrok    # or force one
   ```
   It prints a **PUBLIC LINK** box:
   ```
   PUBLIC LINK (works from anywhere, even with your VPN on):
       Shortcut server address:  https://your-name.ngrok-free.app
       Test on the iPhone:       https://your-name.ngrok-free.app/health
       Token: Bearer abcd…
   ```
   That `https://...` link (no `/` at the end) is your **server address**. If it shows a WARNING about
   the token, fix `.env` and restart.
4. On the iPhone, open `<server address>/health` in Safari. You should see
   `{"ok":true,"device_default":"phone","emulator_connected":true}`
   (`emulator_connected: false` just means the Android emulator isn't running yet).
   On ngrok, Safari may first show an ngrok "You are about to visit" page: tap **Visit Site**.
   The Shortcut skips that page with a header (below).

No tunnel? On the same Wi-Fi you can use the `http://192.168.x.x:8000` address the server prints instead.
iOS will ask "Allow hands-free mode to connect to 192.168.x.x?" the first time: tap **Always Allow**.

Every request needs your token, so strangers who find the public link only get `401`.

## 1. Create the shortcut

1. Open **Shortcuts** -> **+** (top right).
2. Tap the name at the top -> **Rename** -> type `hands-free mode` -> **Done**.

## 2. Save your address and token (type them only once)

1. Add **Text**. Type your server address, e.g. `https://your-name.ngrok-free.app`.
2. Add **Set Variable**. Name it `Server`.
3. Add **Text**. Type `Bearer ` **with one space after it**, then your token,
   e.g. `Bearer pick-a-long-random-string`.
4. Add **Set Variable**. Name it `Auth`.

## 3. The two headers (every URL action gets both)

Every **Get Contents of URL** in this guide (the command, the audio download, and the reply) needs:

| Key | Value |
|---|---|
| `Authorization` | the `Auth` variable |
| `ngrok-skip-browser-warning` | `1` |

Without the second one, ngrok answers with its warning web page instead of Relay's JSON.

## 4. Basic build: listen, send, speak

1. Add **Dictate Text**. Tap the arrow on it: Language = English, **Stop Listening = After Pause**.
2. Add **Get Contents of URL**.
   - URL: insert `Server`, then type `/command` right after it (it reads `Server/command`).
   - **Show More**: Method `POST`. Headers: the two from step 3.
   - Request Body `JSON`: **Add new field** -> **Text** -> Key `text`, Value = **Dictated Text**.
3. Add **Get Dictionary Value**: **Value** for key `say` in **Contents of URL**.
   Add **Set Variable**: name `Say`.
4. Add **Get Dictionary Value**: **Value** for key `audio_url` in **Contents of URL**.
5. **The Text trick.** Add **Text** and put the `audio_url` **Dictionary Value** inside it.
   Then add **If** with input = that **Text**, condition **has any value**.
   (If you point the If straight at the Dictionary Value, Shortcuts sometimes swaps in "File Size"
   and the condition never works. Going through a Text action stops that.)
   - Inside **If**: **Get Contents of URL**, URL = the `audio_url` Text, Method `GET`, the same two
     headers. Then **Play Sound** with input **Contents of URL**.
   - Inside **Otherwise**: **Speak Text** with `Say` (turn on **Wait Until Finished** if you see it).
     This is the built-in iPhone voice, used whenever there's no ElevenLabs audio.
   - **End If** is added for you.
6. After **End If**, add **Nothing**. Without it, the shortcut "returns" the audio file and Siri says
   "something went wrong" after playing it.

Tap play at the bottom of the editor and say "what's on my screen?". That's a working assistant.
Next, the reply loop lets it ask you questions.

### What the server sends back

Every `/command` and `/reply` answer has the same keys:

```json
{"say": "About to tap Send in Messages. Should I do it?",
 "audio_url": "https://your-name.ngrok-free.app/audio/ab12cd34ef56.mp3",
 "needs_reply": true,
 "next": "reply",
 "session_id": "a1b2c3d4"}
```

| Key | Meaning |
|---|---|
| `say` | Plain text to speak, never empty, no markdown. |
| `audio_url` | ElevenLabs mp3 of `say` (GET it with the same headers), or `null` with no ElevenLabs key. |
| `needs_reply` | `true` when Relay asked you a question. |
| `next` | `reply` = listen and send the answer. `wait` = still working, check again. `done` = finished. |
| `session_id` | Send this back to `/reply`. |

`/reply` takes `{"session_id": "...", "text": "..."}`. If the task is still running and `text` is empty,
the server waits up to 45 seconds and answers with the newest update.

## 5. Reply loop (answers "Should I send it?" and keeps checking long tasks)

Relay asks before it sends or pays ("About to tap Send. Should I do it?"), and long tasks answer
"...still working". The loop below speaks each update, listens when there's a question, and keeps
checking until the task is done.

1. Right after the first **Get Contents of URL** (`Server/command`), add **Set Variable** -> `Response`.
2. Change every **Get Dictionary Value** so it reads from `Response` instead of **Contents of URL**.
3. Add **Repeat**, set to **20** times. Drag the whole speak/play block (the `say` and `audio_url`
   Get Values, the Text trick, the If / Otherwise / End If) inside it.
4. Still inside the Repeat, after the speak/play block's **End If**:
   1. **Get Dictionary Value**: **Value** for `next` in `Response`.
   2. **Text** containing that Dictionary Value (same Text trick).
   3. **If** Text **is** `done` -> **Stop This Shortcut**. **End If**.
   4. **If** Text **is** `reply` (use the same Text as input):
      - **Dictate Text** (Stop Listening = After Pause), then **Set Variable** -> `Answer`.
      - **Otherwise**: an empty **Text** action, then **Set Variable** -> `Answer`.
        (An empty answer on a running task means "tell me the next update".)
      - **End If**.
   5. **Get Dictionary Value**: **Value** for `session_id` in `Response`.
   6. **Get Contents of URL**: URL `Server/reply`, Method `POST`, the same two headers,
      Request Body `JSON` with two Text fields: `session_id` = the session_id Dictionary Value,
      `text` = `Answer`.
   7. **Set Variable** -> `Response` (input: this new Contents of URL).
5. **End Repeat**, then keep the **Nothing** as the very last action.

20 rounds of up to 45 seconds each covers about 15 minutes of work, far more than any task needs.

### The finished Shortcut

```
Text "https://your-name.ngrok-free.app"            -> Set Variable Server
Text "Bearer <token>"                              -> Set Variable Auth
Dictate Text
Get Contents of URL  POST Server/command           [Authorization: Auth, ngrok-skip-browser-warning: 1]
                     JSON { text: Dictated Text }
Set Variable Response
Repeat 20 times
    Get Value say in Response                      -> Set Variable Say
    Get Value audio_url in Response -> Text
    If Text has any value
        Get Contents of URL  GET audio_url         [same two headers]
        Play Sound
    Otherwise
        Speak Text Say
    End If
    Get Value next in Response -> Text
    If Text is "done"
        Stop This Shortcut
    End If
    If Text is "reply"
        Dictate Text                               -> Set Variable Answer
    Otherwise
        Text ""                                    -> Set Variable Answer
    End If
    Get Value session_id in Response
    Get Contents of URL  POST Server/reply         [same two headers]
                         JSON { session_id, text: Answer }
    Set Variable Response
End Repeat
Nothing
```

### Optional: a stop button

New shortcut named `hands-free stop`: **Get Contents of URL** -> `Server/stop`, Method `POST`, the same
two headers, then **Nothing**. "Hey Siri, hands-free stop" cancels whatever is running. Saying "stop" or
"cancel" as a normal command works too.

## 6. Launching it

- **Siri**: "Hey Siri, **hands-free mode**". Turn on Settings -> Siri -> **Listen for "Hey Siri"** and
  **Allow Siri When Locked**. With AirPods in, it works with the phone in your pocket.
- **Back Tap**: Settings -> Accessibility -> Touch -> **Back Tap** -> Double Tap -> **hands-free mode**.
- **Action Button** (iPhone 15 Pro and later): Settings -> **Action Button** -> Shortcut ->
  **hands-free mode**.
- **Lock Screen button**: long-press the Lock Screen -> **Customize** -> Lock Screen -> tap a control
  slot -> **Shortcut** -> **hands-free mode**.
- **Apple Watch**: in the Shortcuts app, open the shortcut -> the **(i) Details** button ->
  **Show on Apple Watch**. Then run it from the Shortcuts app on the watch, or add the **Shortcuts
  complication** to your watch face (long-press the face -> Edit -> Complications) for a one-tap button.
- **Wrist mode** (if the watch misbehaves): duplicate the shortcut, name the copy `wrist mode`, and
  delete the audio download (the `audio_url` Get Value, its Text, and the whole If / Otherwise / End If,
  keeping only **Speak Text** `Say`). The watch speaks with its own voice and skips the mp3 download,
  which is the part watches handle worst. Show this copy on the watch instead.

From the lock screen, iOS may still say "unlock your iPhone to continue" for some actions, especially the
first run and sometimes playing audio. If that keeps happening, remove the ElevenLabs key (Speak Text is the
most lock-screen-friendly) or unlock with Face ID when asked.

## Troubleshooting

| What you see | What it means / fix |
|---|---|
| Error **401** / "bad or missing token" | The token or header is wrong. `Auth` must be exactly `Bearer ` + the `RELAY_TOKEN` in `.env`, and the header key must be `Authorization`. The server log says what's off (wrong length, capital letters, which character differs). Restart the server after editing `.env`. |
| "Couldn't convert Rich Text to Dictionary" | The tunnel sent back a **web page**, not JSON. Either the link is stale (Cloudflare links change every restart: copy the new one into the `Server` Text) or the `ngrok-skip-browser-warning: 1` header is missing on one of the URL actions. |
| Siri says "something went wrong" right after the audio plays | Add a **Nothing** action as the very last action (after End If / End Repeat). |
| Siri opens the **Amazon Relay** app | The shortcut is still named "Relay". Rename it `hands-free mode`. |
| The audio If never plays the mp3 (or always fails) | Shortcuts swapped in "File Size". Use the Text trick: put `audio_url` in a **Text** action and point the If at that Text. |
| Error **404** on `/reply` ("unknown session_id") | The server restarted mid-conversation. Start over with "Hey Siri, hands-free mode". |
| `/health` won't load at all | The server or tunnel isn't running, or the link changed. Restart `python -m relay.server --tunnel` and use the link it prints. |
| `/health` works but the Shortcut fails | Check the URL is `Server/command` (no double `//`), Method **POST**, Request Body **JSON** with key `text` (lowercase). |
| Silence, or ElevenLabs audio won't play | Delete `ELEVENLABS_API_KEY` from `.env` and restart: `audio_url` becomes empty and Speak Text is used. |
| `emulator_connected: false` | Start the emulator (Android Studio -> Device Manager -> play) and check `adb devices` lists it. |
| Using the Wi-Fi address and it times out | Phone and Mac must be on the same Wi-Fi, and macOS must allow Python incoming connections (System Settings -> Network -> Firewall -> Options). Easier: use `--tunnel`. |

## Conversation mode (Watch): "Anything else?"

Use `POST /talk` instead of `/command`. Body: `{"text": ..., "session_id": ..., "conversation": true}`.
The reply's `next` tells the Shortcut what to do:

| next  | meaning                                  | Shortcut does            |
|-------|------------------------------------------|--------------------------|
| reply | Relay asked a question ("Send it?")      | Dictate → POST /talk     |
| more  | Done, Rachel asked "Anything else?"      | Dictate → POST /talk     |
| wait  | Still working                            | POST /talk with text ""  |
| done  | Finished ("Okay, talk soon.")            | Stop                     |

Saying "no", "that's all", "I'm good", or nothing ends the conversation.

## Watch: record instead of dictate

On the Apple Watch, "Dictate Text" can open a text box with no microphone, so Relay never
hears you. Instead, record the audio and let the server transcribe it (ElevenLabs Scribe,
falling back to Gemini). Use `POST /talk/audio` in place of `POST /talk`:

1. **Record Audio**: Audio Quality Normal, Start Recording **Immediately**, Finish Recording **On Tap**.
2. **Get Contents of URL**: `https://149-28-41-199.sslip.io/talk/audio?session_id=<Session>&conversation=true`
   - Method **POST**
   - Headers: `Authorization` = `Bearer <token>`
   - Request Body **File**: File = **Recorded Audio**
   - (`<Session>` is the `session_id` from the previous reply; leave it empty on the first turn.)
3. Handle the reply exactly like `/talk`: speak `say` (or play `audio_url`), save `session_id`,
   then branch on `next` (`reply`/`more` -> loop back to step 1, `wait` -> POST `/talk/audio`
   again with no recording or POST `/talk` with text "", `done` -> stop).

The reply also has `heard`: the words the server transcribed (handy for debugging; show it
with Show Result). A recording under about 1 KB counts as silence ("Sorry, I didn't catch that").
Multipart form uploads also work: field `file`, plus optional `session_id`, `conversation`, `device`.
