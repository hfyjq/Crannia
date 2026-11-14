Takeover lends the agent your mouse and keyboard. It is available only in the **desktop app**, and only while this conversation is in **full access**. Both are required; if either is missing the button stays disabled and hovering explains which one is missing.

While takeover is on, the agent can look at windows, click, type, press keys and scroll to carry out an interface task for you. It does not act in the background: each step comes from an instruction in the conversation and happens on the desktop you can see.

## What you see before it starts

- A safety warning stating that you should not interfere with input while the agent is operating the computer.
- The Enable button counts down three seconds and cannot be pressed before it ends.
- The warning offers "I understand": after that the warning stops appearing, but takeover still requires you to press the button every time.

## Who holds control

- **Only one conversation can hold takeover at a time.** A second request is refused with an explanation of who holds it. No queueing, no preemption.
- **Before an application is controlled for the first time, you are asked**: allow for this conversation, allow and remember permanently, or do not allow. Remembered applications go into the Takeover apps list in Settings and can be removed there.
- **One observation maps to one action.** If the window changes between the observation and the click, that action fails outright; it is never retried against a different window.
- When the agent stops operating, the turn ends; the next time it touches the computer, detection starts again.

## What it cannot reach

- **Refused by application**: terminals, sign-in and elevation screens, password managers, system security settings.
- **Refused by key**: the Windows key, Ctrl+Alt+Del, and the emergency-stop key itself (the agent cannot press it).
- **Lock screen or a switch to the secure desktop** stops takeover immediately.
- **Foreground check**: every input is preceded by a check that the target is still in the foreground. If the target runs at a higher integrity level, the agent is told it cannot reach it instead of trying.
- A match means refusal; no match does not mean safe.

## What stops it

- You press the takeover button again.
- You press Esc physically. This is the emergency stop and works at any time.
- You quit from the tray, or switch the mode out of full access.
- While the agent is operating, you use the physical keyboard, mouse, wheel or drag: takeover ends immediately.
- Typing instructions or opening the target application do not count as interference. Detection begins only when the agent actually starts operating, and stops again when the turn ends.

## What stays visible during takeover

A banner stays at the top of the desktop shell, saying that takeover is active and that Esc exits. It stays above other windows and cannot be covered.

That is also why takeover is desktop-only: in a browser there is no such banner, and therefore no such guarantee.

## Deterministic outcomes when something goes wrong

- **The window changed, the foreground changed, or the element is gone**: the action fails and reports back; there is no retry. The agent re-observes and decides again.
- **A second conversation asks for takeover**: refused, naming the current holder.
- **Framework restart or app exit** ends takeover; it must be enabled again.
- **`/clear` does not end takeover**: it clears a conversation's content, not who is holding the mouse.

## Related settings

- **Full access** (the mode button by the composer): a prerequisite for takeover, scoped to the current conversation.
- **Takeover apps** (Settings · Chat): applications approved permanently; removable at any time.
- **Restore safety notices** (Settings · Security): makes "I understand" expire so the warning appears again.
