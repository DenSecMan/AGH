def _delegated_to(result, agent):
    called = [c["args"].get("agent") for c in result.tool_calls if c["name"] == "delegate"]
    return agent in called, f"delegated to {called}"


def delegated_to_researcher(text, result, case):
    return _delegated_to(result, "researcher")


def delegated_to_architect(text, result, case):
    return _delegated_to(result, "architect")


def did_not_delegate_to_architect(text, result, case):
    ok, detail = _delegated_to(result, "architect")
    return not ok, detail
