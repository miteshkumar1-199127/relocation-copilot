from relocation.storage import EphemeralRunIndex, RunIndex


def test_saved_plan_can_reopen_without_workflow(tmp_path):
    index = RunIndex(tmp_path / "runs.sqlite")
    report = {"profile": {"origin": "Bangalore", "destination": "Seoul"}, "tasks": []}
    index.add("move-1", "owner", {"origin": "Bangalore", "destination": "Seoul",
                                   "move_date": "2026-10-24"}, report)
    reopened = RunIndex(tmp_path / "runs.sqlite").get_snapshot("move-1")
    assert reopened == {"report": report, "approval": {}}
    index.save_snapshot("move-1", report, {"decision": "approve"})
    assert RunIndex(tmp_path / "runs.sqlite").get_snapshot("move-1")["approval"]["decision"] == "approve"


def test_saved_move_picker_metadata_follows_profile_edits(tmp_path):
    index = RunIndex(tmp_path / "runs.sqlite")
    original = {"origin": "Bangalore", "destination": "Seoul", "move_date": "2026-10-24"}
    index.add("move-2", "owner", original, {"profile": original})
    revised = {"origin": "Chennai", "destination": "Tokyo", "move_date": "2026-11-02"}
    index.save_snapshot("move-2", {"profile": revised})
    saved = index.list("owner")[0]
    assert (saved["origin"], saved["destination"], saved["move_date"]) == (
        "Chennai", "Tokyo", "2026-11-02")


def test_ephemeral_index_keeps_nothing_outside_its_instance():
    profile = {"origin": "Bangalore", "destination": "Seoul", "move_date": "2026-10-24"}
    first = EphemeralRunIndex()
    first.add("move", "owner", profile, {"profile": profile})
    assert len(first.list("owner")) == 1
    assert EphemeralRunIndex().list("owner") == []


def test_clear_removes_only_the_selected_users_runs(tmp_path):
    index = RunIndex(tmp_path / "runs.sqlite")
    profile = {"origin": "Bangalore", "destination": "Seoul", "move_date": "2026-10-24"}
    index.add("one", "owner", profile, {"profile": profile})
    index.add("two", "someone-else", profile, {"profile": profile})
    index.clear("owner")
    assert index.list("owner") == []
    assert index.get_snapshot("one") is None
    assert len(index.list("someone-else")) == 1
