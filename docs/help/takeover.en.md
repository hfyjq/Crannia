In takeover mode, the agent can interact with on-screen elements, including but not limited to clicking and typing. Takeover mode can only be enabled while the current conversation is in full access. It currently works on Windows desktops only; enabling it on other systems fails with an error.

## Control

Only one conversation can hold takeover at a time. Before an application is controlled for the first time, you are asked in advance: allow (this conversation), allow and approve permanently, or do not allow. Permanently approved entries can be managed in the Takeover apps list on the settings page.

## Hard limits

The following are the direct limits of takeover mode.

- Refused by application: terminals, sign-in and elevation screens, password managers, system security settings.
- Refused by key: the Windows key, Ctrl+Alt+Del, and the emergency-stop key itself (the agent cannot press it).
- Lock screen, or a switch to the secure desktop: stops immediately.
- Foreground check: every input is preceded by a check that the target is still in the foreground; if the target window runs at a higher privilege level than our own, that is reported.

## How to stop

- Click the takeover button again.
- Press Esc physically — the emergency stop.
- Quit from the tray, or switch the mode out of full access.
- While the agent is operating, use the physical keyboard, mouse, wheel or drag: takeover exits immediately.

## Related rules

- Window change, foreground change, element missing, and the like: the action fails and is reported to the agent, which re-observes and decides again.
- A second takeover request: refused, naming the current holder.
- Framework restart or app exit: takeover ends with it.
- `/clear` does not end takeover.

## Related settings

- Full access (the mode button by the composer): a prerequisite for takeover, scoped to the current conversation.
- Takeover apps (Settings · Chat): the list of permanently approved applications.
- Restore safety notices (Settings · Security): restores the safety warning for enabling takeover mode.
