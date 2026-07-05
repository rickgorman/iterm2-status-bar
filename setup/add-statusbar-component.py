#!/usr/bin/env python3
"""Add an Interpolated String component showing user.note to the iTerm2 status
bar of the profile used by the current session ($ITERM_SESSION_ID).

Idempotent: skips if a component already references user.note.
"""
import os

import iterm2

EXPRESSION = "🎯 \\(user.note?)"

NOTE_COMPONENT = {
    "class": "iTermStatusBarSwiftyStringComponent",
    "configuration": {
        "knobs": {
            "expression": EXPRESSION,
            "base: priority": 10,
            "base: compression resistance": 1,
        },
        "layout advanced configuration dictionary value": {
            "auto-rainbow style": 0,
            "algorithm": 1,
            "remove empty components": False,
        },
    },
}


async def main(connection):
    app = await iterm2.async_get_app(connection)
    session_id = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id != session_id:
                    continue
                profile = await session.async_get_profile()
                layout = profile.all_properties.get("Status Bar Layout") or {
                    "components": [],
                    "advanced configuration": {},
                }
                components = layout.get("components", [])
                if any(
                    "user.note" in str(c.get("configuration", {}).get("knobs", {}))
                    for c in components
                ):
                    print("user.note component already present; nothing to do")
                    return
                components.append(NOTE_COMPONENT)
                layout["components"] = components
                # Write to the underlying profile so every tab gets it.
                partials = await iterm2.PartialProfile.async_query(connection)
                for partial in partials:
                    if partial.guid == profile.original_guid:
                        full = await partial.async_get_full_profile()
                        # No public setter for the layout dict in the Python
                        # API; _async_simple_set is what the generated setters
                        # wrap anyway.
                        await full._async_simple_set("Status Bar Layout", layout)
                        print("added user.note component to profile:", full.name)
                        return
                print("could not find underlying profile", profile.original_guid)
                return
    print("session not found; run from inside an iTerm2 tab")


iterm2.run_until_complete(main)
