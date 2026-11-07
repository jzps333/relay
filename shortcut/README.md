# Building the "Relay" iPhone Shortcut

The Shortcut is the whole iPhone app: it listens, sends the words to the Mac, and speaks the answer.
Build it in the **Shortcuts** app. Every action name below is exactly what you type in the action search bar.

You need:
- The Mac's address, e.g. `http://192.168.1.20:8000` (same Wi-Fi) or `http://relay-mac:8000` (Tailscale, see below).
  Find the Wi-Fi IP on the Mac: System Settings -> Wi-Fi -> Details -> IP address.
- Your `RELAY_TOKEN` from `.env`.

## 1. Create it

1. Shortcuts -> **+** (top right). Tap the name at the top -> **Rename** -> `Relay`.
   (The name is what Siri listens for: "Hey Siri, Relay".)

## 2. Set up two variables (so you only type them once)

2. Add **Text**. Type your server address, e.g. `http://192.168.1.20:8000` (no slash at the end).
3. Add **Set Variable**. Variable name: `Server`. Input: the Text above.
4. Add **Text**. Type `Bearer ` (with a space) followed by your token, e.g. `Bearer pick-a-long-random-string`.
5. Add **Set Variable**. Name: `Auth`.

## 3. Listen and send

6. Add **Dictate Text**. Tap the arrow: Language = English, **Stop Listening = After Pause**.
7. Add **Get Contents of URL**.
   - URL: tap and insert `Server`, then type `/command` right after it.
   - Tap **Show More**: Method = **POST**.
   - Headers: **Add new header** -> Key `Authorization`, Value = the `Auth` variable.
   - Request Body = **JSON**. Add field: Type **Text**, Key `text`, Value = **Dictated Text** (magic variable).
   - (Optional) Add field Key `device`, Text `mac` — only in a second "Relay Mac" shortcut. Leave it out
     here; the server guesses from words like "on my Mac".
8. Add **Set Variable**. Name: `Response`. Input: Contents of URL.

## 4. Speak the answer, then keep going while it has a question

9. Add **Repeat**. Set it to **10** times. (Repeat = "keep answering questions"; we exit early when done.)
   Everything in steps 10-19 goes **inside** the Repeat.
10. Add **Get Dictionary Value**. Get **Value** for key `say` in `Response`. Then **Set Variable** `Say`.
11. Add **Get Dictionary Value**: key `audio_url` in `Response`.
12. Add **If**: Input = that Dictionary Value, condition **has any value**.
    - Inside the If: **Get Contents of URL** -> URL = the Dictionary Value (`audio_url`),
      Method GET, header `Authorization` = `Auth`. Then **Play Sound** (input: Contents of URL).
    - **Otherwise**: **Speak Text** with `Say`. (This is the fallback when no ElevenLabs key is set.
      Tap the arrow and turn **Wait Until Finished** on.)
    - **End If**.
13. Add **Get Dictionary Value**: key `needs_reply` in `Response`.
14. Add **If** that value **is** `1` (Shortcuts shows JSON `true` as 1; if your iOS shows "Yes/No",
    pick **is true**).
15. Inside it: **Dictate Text** (Stop Listening = After Pause).
16. **Get Dictionary Value**: key `session_id` in `Response`.
17. **Get Contents of URL**: URL = `Server` + `/reply`, Method **POST**, header `Authorization` = `Auth`,
    JSON body: `session_id` = the session_id value, `text` = the new Dictated Text.
18. **Set Variable** `Response` = that Contents of URL (overwrites the old one, so the next loop speaks it).
19. **Otherwise**: add **Stop This Shortcut** (no question means we're finished). **End If**.
20. **End Repeat** is added for you.

Tip: if a long task replies "I'm still working on it", just answer anything like "okay" — any `/reply`
on a running task waits another 25 seconds and gives you the newest update.

## 5. Optional: a "Relay Stop" shortcut

New shortcut named `Relay Stop`: **Get Contents of URL** -> `Server` + `/stop`, POST, `Authorization` header.
Then "Hey Siri, Relay Stop" kills whatever is running. (Saying "stop" as a new command also works.)

## 6. "Hey Siri, Relay"

- Settings -> Siri -> **Listen for "Hey Siri"** (or "Siri") on. The shortcut name is the trigger, so just say
  **"Hey Siri, Relay"**. With AirPods in, that works with the phone in your pocket.
- Settings -> Siri -> **Allow Siri When Locked** on, so it works from the lock screen.
- Extra hands-free options: Settings -> Accessibility -> Touch -> **Back Tap** -> Double Tap -> Relay,
  or iPhone 15 Pro and later: Settings -> **Action Button** -> Shortcut -> Relay.
- First run: iOS asks "Allow Relay to connect to 192.168.x.x?" — tap **Always Allow** (do this once
  with someone sighted nearby, or with VoiceOver).

## 7. Tailscale: works when the phone is off the Mac's Wi-Fi

Tailscale makes a private network between your devices, so the phone reaches the Mac from cellular
or any Wi-Fi without opening ports on the router.

1. Mac: install Tailscale (tailscale.com/download or the Mac App Store), sign in.
2. iPhone: install **Tailscale** from the App Store, sign in with the **same account**, turn the VPN toggle on.
3. In the Tailscale admin page (login.tailscale.com -> Machines) find the Mac's name or `100.x.y.z` IP.
   Turn on **MagicDNS** (DNS tab) so you can use the name.
4. In the Shortcut, change the `Server` Text (step 2) to `http://<mac-name>:8000` or `http://100.x.y.z:8000`.
   This works on home Wi-Fi too, so you only need one version of the shortcut.
5. Make sure the server listens on all interfaces: `uvicorn relay.server:app --host 0.0.0.0 --port 8000`.
   If macOS asks to allow incoming connections for Python, click **Allow**.
6. Keep the Mac awake: System Settings -> Battery/Energy -> "Prevent automatic sleeping when the display
   is off", or run `caffeinate -dis` in a terminal.

Keep `RELAY_TOKEN` long and random even on Tailscale; this server can tap, type and call on your behalf.
