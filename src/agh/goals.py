"""/goal: keep the agent working on one objective, turn after turn, until it says the goal is met.

Before the first turn the user is asked for a token budget. When the goal's turns have spent it, the loop pauses and
asks whether to add more or stop. The agent ends its final message with a marker line to finish or give up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DONE_MARK = "GOAL COMPLETE"
BLOCKED_MARK = "GOAL BLOCKED"
NO_BUDGET = ("none", "no", "n", "unlimited", "skip", "0", "")
STOP_WORDS = ("stop", "s", "no", "n", "cancel", "quit", "end")

ASK_BUDGET = ("Is there a token budget this goal must be completed within? Reply with a number such as 200000, 200k "
              "or 1.5m, or 'none' for no limit. (/goal stop cancels.)")


class BudgetError(ValueError):
    pass


def parse_tokens(text: str) -> int | None:
    """'200k' -> 200000, '1.5m' -> 1500000, '50,000' -> 50000, 'none' -> None."""
    t = text.strip().lower().replace(",", "").replace("_", "").removesuffix("tokens").strip()
    if t in NO_BUDGET:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", t)
    if not m:
        raise BudgetError(f"Couldn't read {text.strip()!r} as a token count; try 200000, 200k or 1.5m.")
    value = int(float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)])
    if value <= 0:
        return None
    return value


@dataclass
class Goal:
    objective: str
    budget: int | None = None
    spent: int = 0
    turns: int = 0
    state: str = "awaiting_budget"  # awaiting_budget | running | awaiting_more | complete | blocked | stopped
    note: str = ""

    @property
    def waiting_for_user(self) -> bool:
        return self.state in ("awaiting_budget", "awaiting_more")

    @property
    def active(self) -> bool:
        return self.state in ("awaiting_budget", "running", "awaiting_more")

    def describe(self) -> str:
        budget = f"{self.spent:,} / {self.budget:,} tokens" if self.budget else f"{self.spent:,} tokens (no budget)"
        line = f"Goal ({self.state.replace('_', ' ')}): {self.objective}\n{self.turns} turns, {budget}"
        return line + (f"\n{self.note}" if self.note else "")

    def first_prompt(self) -> str:
        return (f"You have a goal to complete autonomously:\n\n{self.objective}\n\n"
                "Work toward it now. You will be prompted to continue after each reply, so keep going across turns "
                "without waiting for the user. When the goal is fully achieved and verified, end your final message "
                f"with a line containing exactly {DONE_MARK}. If it cannot be achieved without the user's help, end "
                f"with a line `{BLOCKED_MARK}: <reason>`. Don't write either line otherwise.")

    def continue_prompt(self) -> str:
        left = f" About {self.budget - self.spent:,} tokens of budget remain." if self.budget else ""
        return (f"Continue working on the goal: {self.objective}\n{left}\nIf it is done and verified, end with "
                f"{DONE_MARK}; if you are stuck and need the user, end with `{BLOCKED_MARK}: <reason>`.")

    def answer(self, text: str) -> str:
        """The user's reply to a budget question. Returns what to tell them; state says what happens next."""
        reply = text.strip()
        if self.state == "awaiting_budget":
            self.budget = parse_tokens(reply)
            self.state = "running"
            return (f"Budget set to {self.budget:,} tokens." if self.budget else "No token budget.") + \
                " Working on the goal; /goal shows progress, /goal stop or Ctrl+C ends it."
        if self.state == "awaiting_more":
            if reply.lower() in STOP_WORDS:
                self.state, self.note = "stopped", "Stopped at the token budget."
                return f"Goal stopped after {self.spent:,} tokens."
            extra = parse_tokens(reply.lower().removeprefix("add").strip())
            if extra is None:
                self.budget = None
                self.state = "running"
                return "Budget removed. Continuing the goal."
            self.budget = max(self.budget or 0, self.spent) + extra
            self.state = "running"
            return f"Added {extra:,} tokens (budget now {self.budget:,}). Continuing the goal."
        raise BudgetError("The goal isn't waiting for an answer.")

    def after_turn(self, tokens: int, text: str, stop_reason: str = "") -> str:
        """Record a finished turn and decide what's next: continue | complete | blocked | budget | stopped."""
        self.spent += tokens
        self.turns += 1
        last = [ln.strip().strip("*`_ ") for ln in (text or "").strip().splitlines()[-3:]]
        if any(ln.upper().startswith(BLOCKED_MARK) for ln in last):
            reason = next(ln for ln in last if ln.upper().startswith(BLOCKED_MARK))[len(BLOCKED_MARK):]
            self.state, self.note = "blocked", "Blocked: " + (reason.lstrip(": ").strip() or "no reason given")
        elif any(ln.upper() == DONE_MARK for ln in last):
            self.state, self.note = "complete", ""
        elif stop_reason in ("cancelled", "error"):
            self.state, self.note = "stopped", f"Stopped: the turn ended with {stop_reason}."
        elif self.budget and self.spent >= self.budget:
            self.state = "awaiting_more"
            return "budget"
        else:
            return "continue"
        return self.state

    def budget_question(self) -> str:
        return (f"The goal has used {self.spent:,} of its {self.budget:,} token budget and isn't finished. Add more "
                "budget (e.g. 100k, or 'none' for no limit) or 'stop'?")
