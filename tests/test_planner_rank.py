from __future__ import annotations

from brain.contracts import AppInfo, UIItem, UISnapshot
from brain.planner.rank import match_apps, match_score, rank_items, truncate, words
from brain.planner.testing import notes_snapshot


def test_words_drops_stopwords_and_punctuation() -> None:
    assert words("Open the Notes, and call it Standup!") == ["open", "notes", "call", "standup"]


def test_match_score_prefers_overlapping_names() -> None:
    utt = "start a new note called standup"
    assert match_score(utt, "New Note") > match_score(utt, "Show Folders")
    assert match_score(utt, "") == 0.0
    assert match_score("", "New Note") == 0.0


def test_rank_puts_relevant_items_first() -> None:
    ranked = rank_items("make it bold", notes_snapshot(), k=3)
    assert ranked[0].id == 104


def test_rank_excludes_disabled_items() -> None:
    ranked = rank_items("share this note", notes_snapshot(), k=50)
    assert 4 not in {it.id for it in ranked}


def test_rank_respects_k() -> None:
    assert len(rank_items("anything", notes_snapshot(), k=4)) == 4
    assert rank_items("anything", notes_snapshot(), k=0) == []


def test_always_useful_menu_beats_unrelated_menu() -> None:
    ranked = [it.id for it in rank_items("hmm", notes_snapshot(), k=50)]
    assert ranked.index(103) < ranked.index(105)  # Edit › Undo is always useful


def test_focused_element_gets_bonus() -> None:
    ranked = [it.id for it in rank_items("hmm", notes_snapshot(), k=50)]
    assert ranked.index(7) < ranked.index(2)


def test_rank_is_stable_for_ties() -> None:
    snap = UISnapshot(
        "s",
        AppInfo("X"),
        items=tuple(UIItem(i, "element", "button", "") for i in range(5)),
    )
    assert [it.id for it in rank_items("go", snap)] == [0, 1, 2, 3, 4]


def test_match_apps() -> None:
    apps = ["Notes", "Slack", "Safari", "System Settings", "Calendar"]
    assert match_apps("open slack please", apps)[0] == "Slack"
    assert "Notes" in match_apps("open notes and start a note", apps)
    assert match_apps("open slack", []) == []
    assert match_apps("the", apps) == []
    assert match_apps("open system settings", apps)[0] == "System Settings"


def test_match_apps_is_case_insensitive_and_not_loose() -> None:
    assert match_apps("open tv", ["TV", "Notes"]) == ["TV"]
    assert match_apps("start obs", ["OBS"]) == ["OBS"]
    assert match_apps("set a timer for ten minutes", ["Notes", "Calendar"]) == []


def test_truncate() -> None:
    assert truncate("short") == "short"
    long = "x" * 60
    out = truncate(long)
    assert len(out) == 40 and out.endswith("…")
    assert truncate("a   b\n c") == "a b c"


def test_concepts_bring_settings_panes_into_view() -> None:
    snap = UISnapshot(
        "s",
        AppInfo("System Settings"),
        items=(
            UIItem(15, "element", "row", "Appearance"),
            UIItem(22, "element", "row", "Sound"),
            UIItem(10, "element", "row", "Wi-Fi"),
            *(UIItem(100 + i, "menu", path=("Edit", f"Thing {i}"), shortcut="cmd+x") for i in range(20)),
        ),
    )
    assert rank_items("turn on dark mode", snap, k=5)[0].id == 15
    assert rank_items("turn off wifi", snap, k=5)[0].id == 10
    assert rank_items("mute the volume", snap, k=5)[0].id == 22


def test_element_quota_keeps_elements_among_many_menus() -> None:
    menus = tuple(UIItem(100 + i, "menu", path=("Edit", "Undo"), shortcut="cmd+z") for i in range(20))
    elems = tuple(UIItem(i, "element", "row", f"Row {i}") for i in range(8))
    snap = UISnapshot("s", AppInfo("X"), items=menus + elems)
    ranked = rank_items("hmm", snap, k=15)
    assert sum(it.kind == "element" for it in ranked) == 5
    assert sum(it.kind == "element" for it in rank_items("hmm", snap, k=3)) == 1
