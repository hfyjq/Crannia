## About automatic mode

For long-running or many-step tasks, crannia provides automatic mode to improve your experience. It has two layers: the boundary gate and automatic review. The boundary gate runs before automatic review and stops any behavior that crosses a hard boundary; what gets through the gate is handed to automatic review. If automatic review judges an action safe, the action is allowed; otherwise it is rejected.

==Note: even in automatic mode, you remain the first person responsible for the safety of your production environment.==

## The two layers

- **Boundary gate** (the "gate"): an action that hits the gate is either rejected deterministically or escalated to a question for you. Deterministic rejection covers, for example, network commands (the curl, wget, ssh, scp, nc, rsync family); when a command mentions a protected path, it is escalated to a question for you.
- **Automatic review**: a reviewer model examines the action request. If the reviewer considers an action destructive, harmful, or irreversible, it blocks the action. The review is not fixed: if you state boundaries or requirements in your conversation with crannia, those statements also become references for automatic review. Note that they have no absolute binding force, because they exist only in the conversation; if you want them guaranteed, write the entries into Settings · Security → Automatic review rules.

## Order of decisions

1. Gate: a hit is rejected, or escalated to a question for you.
2. You approved the same thing this time or in this conversation: allow.
3. A write operation, reversible, and all targets inside the workspace: allow.
4. Read-only tools: allow.
5. None of the above: hand over to automatic review.

## Other boundaries of automatic mode

- **Breaker**: after {breaker_consecutive} consecutive or {breaker_total} total blocks by automatic review, the next action is forced to ask you, and the count is reset. The current conversation is not interrupted by this.
- **No response**: in automatic mode, the approval wait defaults to {approval_timeout_seconds} seconds and can be changed in Settings. If there is still no response when the countdown ends, the model receives a "no channel available" receipt and continues working; this is not treated as a rejection.
- **Unable to judge**: there are times when automatic review cannot tell whether an action is safe or dangerous; such cases are always treated as dangerous actions, but they do not count toward the breaker.
- **Not absolutely safe**: automatic review is not absolutely safe either; it cannot tell whether the result of an action is real. Please keep an eye on the safety of your production environment.
- **Network connections**: some network connection behavior is blocked; this does not count toward the breaker.
- **Allow all**: note that the "allow all" option does not remove the boundary gate.
- **Actions that need explicit consent**: cross-zone search, deletion, writing outside the workspace, per-application authorization. These still prompt you.
- **Irreversible actions**: they still ask. For example, a large batch deletion is not executed just because automatic review considers it "safe".

## Related settings

- **Default mode** (Settings · Chat): which mode new conversations start in. It only affects new conversations; the current conversation is changed with the button on the input box.
- **Approval timeout** (Settings · Chat): how long a question stays pending in automatic mode; 0 means wait until you answer.
- **Access and command lists** (Settings · Security): approved domains and commands.
- **Automatic review rules** (Settings · Security): write down the entries you want automatic review to follow long-term; you can add, remove, or edit them one by one.
- **Relax dangerous commands** (Settings · Operation): lowers the review level of dangerous behavior, meaning a "must ask" question becomes "ask once, then it can be remembered". The first execution still asks.
