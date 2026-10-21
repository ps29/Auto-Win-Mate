r"""Unit test for the agent's repeated-failure guard and the stale element id errors. No windows or input.

Run from the WinPilot folder:
    ..\WinControlMCP\.venv\Scripts\python.exe tests\repeat_guard_test.py
"""
import queue
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pilot  # noqa: E402


def make_agent():
    return pilot.Agent(types.SimpleNamespace(events=queue.Queue(), log=None))


def failing(text="boom"):
    return {"id": "1", "text": text, "images": [], "error": True}


def test_identical_failures_warn_then_stop():
    agent = make_agent()
    args = {"element_id": 38}
    for n in range(1, 3):
        res = failing()
        agent._note_repeat_failure(res, "click_element", args)
        assert res["text"] == "boom" and agent.abort_reason is None, n
    res = failing()
    agent._note_repeat_failure(res, "click_element", args)  # third time: warned
    assert "failed 3 times" in res["text"] and "get_ui_elements" in res["text"], res["text"]
    assert agent.abort_reason is None
    for _ in range(3):
        agent._note_repeat_failure(failing(), "click_element", args)
    assert agent.abort_reason and "click_element failed 6 times" in agent.abort_reason, agent.abort_reason


def test_other_calls_dont_hide_a_loop_but_success_resets():
    agent = make_agent()
    ok = {"id": "2", "text": "ok", "images": [], "error": False}
    args = {"element_id": 38}
    for n in range(1, 4):  # fail, succeed elsewhere, fail, ...: an A-B-A-B loop is still caught
        res = failing()
        agent._note_repeat_failure(res, "click_element", args)
        agent._note_repeat_failure(dict(ok), "list_windows", {})
    assert agent.fail_streak[("click_element", '{"element_id": 38}')] == 3
    agent._note_repeat_failure(failing(), "click_element", {"element_id": 5})  # different arguments: own count
    assert agent.fail_streak[("click_element", '{"element_id": 5}')] == 1
    agent._note_repeat_failure(dict(ok), "click_element", args)  # the same call worked: forget its failures
    assert ("click_element", '{"element_id": 38}') not in agent.fail_streak
    res = failing()
    agent._note_repeat_failure(res, "click_element", args)
    assert res["text"] == "boom"


def test_stale_element_errors_say_what_to_do():
    server = pilot.server
    server._ELEMENTS.clear()
    try:
        server._element(38)
        raise AssertionError("an unknown id should fail")
    except ValueError as e:
        assert "Unknown element id 38" in str(e) and "get_ui_elements" in str(e), e
    server._ELEMENTS[:] = [(object(), 0x7FFFFFF0)]  # a window handle that doesn't exist
    try:
        server._element(0)
        raise AssertionError("a closed window should fail")
    except ValueError as e:
        assert "stale" in str(e) and "get_ui_elements" in str(e), e
    finally:
        server._ELEMENTS.clear()


def test_unverified_input_is_tracked():
    agent = make_agent()
    agent._run_tool = lambda call_id, name, args: {"id": call_id, "text": "ok", "images": [], "error": False}
    agent._run_calls([("1", "click_element", {"element_id": 1})])
    assert agent.unverified, "an input action nobody looked at must be flagged"
    agent._run_calls([("2", "get_ui_elements", {"window": "x"})])
    assert not agent.unverified, "looking at the result clears it"
    agent._run_calls([("3", "type_text", {"text": "a"}), ("4", "list_windows", {})])
    assert not agent.unverified
    agent._run_calls([("5", "press_keys", {"keys": "enter"})])
    assert agent.unverified
    agent._run_tool = lambda call_id, name, args: {"id": call_id, "text": "no", "images": [], "error": True}
    agent._run_calls([("6", "click_element", {"element_id": 1})])
    assert not agent.unverified, "a failed action was already reported to the model"


def test_repeated_actions_that_work_get_a_note():
    agent = make_agent()
    for n in range(1, 9):
        res = {"id": "1", "text": "Clicked.", "images": [], "error": False}
        agent._note_repeat_failure(res, "click_element", {"element_id": 7})
        if n in (4, 8):
            assert f"{n} times" in res["text"] and "not working" in res["text"], (n, res["text"])
        else:
            assert res["text"] == "Clicked.", (n, res["text"])
    res = {"id": "2", "text": "listed", "images": [], "error": False}
    for _ in range(10):  # looking is not an action: repeating it is normal
        agent._note_repeat_failure(res, "get_ui_elements", {"window": "x"})
    assert res["text"] == "listed"


if __name__ == "__main__":
    test_identical_failures_warn_then_stop()
    test_other_calls_dont_hide_a_loop_but_success_resets()
    test_stale_element_errors_say_what_to_do()
    test_unverified_input_is_tracked()
    test_repeated_actions_that_work_get_a_note()
    print("PASS")
