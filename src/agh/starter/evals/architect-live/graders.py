PLANNING = {"update_todos", "recall"}


def _names(result):
    return [c["name"] for c in result.tool_calls if c["name"] not in PLANNING]


def lists_elements_first(text, result, case):
    names = _names(result)
    return names[:1] == ["list_elements"], f"calls: {names}"


def loads_build_tool_before_creating(text, result, case):
    calls = [c for c in result.tool_calls if c["name"] not in PLANNING]
    names = [c["name"] for c in calls]
    if "create_tool" not in names:
        return False, f"never tried create_tool: {names}"
    loaded = [c["args"].get("name") for c in calls[:names.index("create_tool")] if c["name"] == "load_skill"]
    return "build-tool" in loaded, f"skills loaded first: {loaded}"


def creates_tool_before_agent(text, result, case):
    names = _names(result)
    if "create_tool" not in names:
        return False, f"never tried create_tool: {names}"
    return "create_agent" not in names or names.index("create_tool") < names.index("create_agent"), f"calls: {names}"
