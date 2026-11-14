from rich.markdown import Markdown
from rich.text import Text

from agh.session_log import SessionLog


def test_session_log_resets_and_appends_plain_text(tmp_path):
    log = SessionLog(tmp_path / "session.log")
    log.reset("agent=a")
    log.write("user", Text("> hello"))
    log.write("agent", Markdown("**hi**\nthere"))
    log.write("notice", "")  # empty entries are skipped
    text = log.path.read_text(encoding="utf-8")
    assert "agent=a" in text and "user: > hello" in text and "agent: **hi**\n    there" in text
    assert "notice" not in text
    log.reset()
    assert "hello" not in log.path.read_text(encoding="utf-8")
