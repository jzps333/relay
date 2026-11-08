"""Per-app "playbooks": short navigation tips for apps Relay knows well.

When one of these apps is in the foreground, pick_action (relay/brains/claude.py)
adds its tips to the prompt so Claude can find the right buttons in fewer steps.

Tips are HINTS, not guarantees: apps change their layout between versions, so
they say "usually" and "look for". Claude must still trust the real screen.

Keys are Android package names (the same ones as APP_PACKAGES in
relay/devices/phone.py), because that is what Screen.app holds on the phone.
"""
from __future__ import annotations

# One shared safety line, reused in apps that often show login screens.
NO_SIGN_IN = "- Never sign in or type passwords; ask the user."

TIPS: dict[str, str] = {
    # Instagram
    "com.instagram.android": "\n".join([
        "- DMs: on the home feed, look for the messenger / paper-plane icon at the top right.",
        "- To find a chat, use the search box at the top of the DM list and type the person's name.",
        "- In a chat, the text box is usually labelled \"Message...\"; tap it, type, then tap Send.",
        "- The bottom bar usually has Home, Search, Reels, and Profile tabs.",
        "- To find a person, use the plain Search field and pick their account from the results. "
        "Avoid \"Search with Meta AI\" / \"Ask Meta AI\": that's a chatbot, not people search.",
        "- On a profile, \"Message\" / \"Send message\" just opens the chat; nothing is sent until Send.",
        NO_SIGN_IN,
    ]),
    # WhatsApp
    "com.whatsapp": "\n".join([
        "- The Chats tab lists conversations; look for a search (magnifier) icon or bar at the top.",
        "- Search the contact's name, then tap their chat to open it.",
        "- The message box usually says \"Message\"; after typing, the mic button turns into a Send arrow.",
        "- To start a new chat, look for the round new-chat button at the bottom right.",
    ]),
    # Gmail
    "com.google.android.gm": "\n".join([
        "- To write an email, look for the \"Compose\" button at the bottom right of the inbox.",
        "- In Compose, fill To, Subject, then the body (\"Compose email\"), then tap the Send arrow at the top.",
        "- The search bar at the top (\"Search in mail\") finds emails by sender or words.",
        "- Tap an email in the list to open it; Reply is usually at the bottom of the message.",
        NO_SIGN_IN,
    ]),
    # Google Maps
    "com.google.android.apps.maps": "\n".join([
        "- Use the \"Search here\" box at the top to find a place or address.",
        "- On a place's card, look for a \"Directions\" button, then pick a mode (car, transit, walk).",
        "- Tap \"Start\" to begin turn-by-turn navigation.",
        "- If asked for location permission, allow it; directions need it.",
    ]),
    # Spotify
    "com.spotify.music": "\n".join([
        "- The bottom bar usually has Home, Search, and Your Library tabs.",
        "- To play something specific, use the Search tab, type the song/artist, and tap the result.",
        "- Liked Songs is usually near the top of Your Library.",
        "- On a playlist or Liked Songs, look for the shuffle button next to the big Play button.",
        NO_SIGN_IN,
    ]),
    # DoorDash
    "com.dd.doordash": "\n".join([
        "- NEVER place an order. Stop at the checkout screen and use ask_user so Relay asks the user first.",
        "- Use the search (magnifier) tab or bar to find a restaurant or dish.",
        "- On a store page, tap an item, then look for \"Add to cart\".",
        "- The cart is usually a button at the bottom or top right once items are added.",
        NO_SIGN_IN,
    ]),
    # Google Messages
    "com.google.android.apps.messaging": "\n".join([
        "- To start a text, look for the \"Start chat\" button at the bottom right.",
        "- Type the contact's name or number in the \"To\" box, then tap the matching contact.",
        "- The message box usually says \"Text message\" or \"RCS message\"; tap Send (arrow) after typing.",
        "- Existing conversations are listed on the main screen; tap one to open it.",
    ]),
    # Android Settings
    "com.android.settings": "\n".join([
        "- The fastest way is usually the search bar at the top (\"Search settings\").",
        "- Common sections: Network & internet, Connected devices, Display, Sound, Battery, Accessibility.",
        "- Switches show [on]/[off]; check the state before and after tapping.",
        "- Use back to return to the previous menu.",
    ]),
    # YouTube
    "com.google.android.youtube": "\n".join([
        "- Look for the search (magnifier) icon at the top right, type the query, then pick a result.",
        "- Tap a video to play it; tap the video area to show play/pause controls.",
        "- The bottom bar usually has Home, Shorts, Subscriptions, and You (library).",
        "- Ads may play first; look for a \"Skip\" button after a few seconds.",
    ]),
    # Chrome
    "com.android.chrome": "\n".join([
        "- The address bar at the top doubles as search; tap it, type a site or query, then press enter.",
        "- Tabs: look for the square tab-count button next to the address bar.",
        "- The three-dot menu at the top right has New tab, History, and Bookmarks.",
        "- Close pop-ups or cookie banners first if they cover the page.",
        NO_SIGN_IN,
    ]),
    # LinkedIn
    "com.linkedin.android": "\n".join([
        "- To find a person, tap the Search bar at the top, type their full name, and pick the "
        "People result that matches (check the headline/school/company if several match).",
        "- On their profile, \"Connect\" may be a button or hidden under \"More\" (three dots). "
        "Relay asks the user before tapping Connect.",
        "- If LinkedIn offers \"Add a note\", choose \"Send without a note\" unless the user gave a note.",
        "- \"Follow\" is not the same as \"Connect\"; prefer Connect when the user says connect.",
        NO_SIGN_IN,
    ]),
}


def tips_for(package: str) -> str:
    """Return the tips for an app's package name, or "" if we have none."""
    return TIPS.get(package, "")
