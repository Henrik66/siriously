"""Tests for brain.fixtures: fixture files, FixtureUIProvider, FakeExecutor, key normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.contracts import Action, UIProvider, UISnapshot
from brain.contracts import Executor as ExecutorProtocol
from brain.fixtures import (
    DEFAULT_FIXTURES_DIR,
    FakeExecutor,
    Fixture,
    FixtureError,
    FixtureUIProvider,
    fixture_names,
    load_apps,
    load_fixture,
    normalize_keys,
    synthetic_fixture,
    validate_fixture,
)

REQUIRED_APPS = {"Finder", "Notes", "System Settings", "Calendar", "Slack", "TextEdit"}


def _menu(snap: UISnapshot, *path: str) -> tuple[int, str | None]:
    item = next(i for i in snap.menus if i.path == path)
    return item.id, item.shortcut


def _exec(fixture: str = "finder", state: str | None = None) -> tuple[FixtureUIProvider, FakeExecutor]:
    p = FixtureUIProvider(fixture, state=state)
    return p, FakeExecutor(p)


# ---------------------------------------------------------------------------
# Shipped fixture files
# ---------------------------------------------------------------------------


def test_required_apps_have_fixtures() -> None:
    apps = {load_fixture(n).app.name for n in fixture_names()}
    assert apps >= REQUIRED_APPS


@pytest.mark.parametrize("name", fixture_names())
def test_fixture_is_valid_and_multi_state(name: str) -> None:
    fx = load_fixture(name)
    assert validate_fixture(fx) == []
    assert 2 <= len(fx.snapshots) <= 6
    assert fx.transitions
    for snap in fx.snapshots.values():
        assert UISnapshot.from_dict(snap.to_dict()) == snap  # exactly the PRD §4.2 shape
        assert snap.app.bundle_id
        assert snap.elements and snap.menus
        assert len({i.app.name for i in fx.snapshots.values()}) == 1


@pytest.mark.parametrize("name", fixture_names())
def test_fixture_has_standard_menus(name: str) -> None:
    snap = load_fixture(name).snapshots[load_fixture(name).initial]
    assert _menu(snap, "Edit", "Undo") == (_menu(snap, "Edit", "Undo")[0], "cmd+z")
    assert _menu(snap, "Window", "Minimize")[1] == "cmd+m"
    if snap.app.name != "Finder":  # Finder has no Quit item
        assert _menu(snap, snap.app.name, f"Quit {snap.app.name}")[1] == "cmd+q"


@pytest.mark.parametrize(
    ("fixture", "path", "shortcut"),
    [
        ("notes", ("File", "New Note"), "cmd+n"),
        ("notes", ("Edit", "Delete"), "cmd+delete"),
        ("finder", ("File", "New Folder"), "shift+cmd+n"),
        ("finder", ("Go", "Downloads"), "opt+cmd+l"),
        ("finder", ("Finder", "Empty Trash…"), "shift+cmd+delete"),
        ("safari", ("File", "New Tab"), "cmd+t"),
        ("slack", ("Go", "Jump to…"), "cmd+k"),
        ("calendar", ("File", "New Event"), "cmd+n"),
        ("textedit", ("File", "Save…"), "cmd+s"),
    ],
)
def test_real_menu_paths_and_shortcuts(fixture: str, path: tuple[str, ...], shortcut: str) -> None:
    fx = load_fixture(fixture)
    assert _menu(fx.snapshots[fx.initial], *path)[1] == shortcut


def test_fixture_round_trips_through_dict() -> None:
    fx = load_fixture("notes")
    assert Fixture.from_dict(json.loads(json.dumps(fx.to_dict()))) == fx


def test_apps_json() -> None:
    apps = load_apps()
    assert len(apps) >= 35
    assert len(set(apps)) == len(apps)
    assert set(apps) >= REQUIRED_APPS


def test_load_apps_accepts_object_form_and_rejects_garbage(tmp_path: Path) -> None:
    (tmp_path / "apps.json").write_text('{"apps": ["Notes", "Finder"]}', encoding="utf-8")
    assert load_apps(tmp_path) == ["Notes", "Finder"]
    (tmp_path / "apps.json").write_text('{"apps": [1, 2]}', encoding="utf-8")
    with pytest.raises(FixtureError):
        load_apps(tmp_path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _mini(**over: object) -> dict[str, object]:
    snap = {
        "snapshot_id": "s-0",
        "app": {"name": "Mini", "bundle_id": "x.mini"},
        "items": [{"id": 1, "kind": "element", "role": "button", "label": "Go"}],
    }
    d: dict[str, object] = {
        "name": "mini",
        "initial": "s-0",
        "snapshots": {"s-0": snap, "s-1": {**snap, "snapshot_id": "s-1"}},
        "transitions": [{"from": "s-0", "action": {"op": "press", "target": 1}, "to": "s-1"}],
    }
    d.update(over)
    return d


def test_valid_minimal_fixture() -> None:
    fx = Fixture.from_dict(_mini())
    assert fx.next_state("s-0", Action.press(1)) == "s-1"
    assert fx.next_state("s-1", Action.press(1)) is None


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"initial": "nope"}, "initial"),
        ({"transitions": [{"from": "s-0", "action": {"op": "press", "target": 9}, "to": "s-1"}]}, "target 9"),
        ({"transitions": [{"from": "s-0", "action": {"op": "press", "target": 1}, "to": "zz"}]}, "unknown"),
        ({"transitions": [{"from": "s-0", "action": {"op": "fly"}, "to": "s-1"}]}, "unknown op"),
        ({"name": None, "snapshots": "bad"}, "fixture"),
    ],
)
def test_invalid_fixtures_raise(over: dict[str, object], needle: str) -> None:
    with pytest.raises(FixtureError, match=needle):
        Fixture.from_dict(_mini(**over))


def test_id_identity_must_be_stable_across_states() -> None:
    d = _mini()
    snaps = d["snapshots"]
    assert isinstance(snaps, dict)
    snaps["s-1"] = {
        **snaps["s-1"],
        "items": [{"id": 1, "kind": "element", "role": "textfield", "label": "Go"}],
    }
    with pytest.raises(FixtureError, match="changes identity"):
        Fixture.from_dict(d)


def test_unknown_fixture_lists_available() -> None:
    with pytest.raises(FixtureError, match="notes"):
        FixtureUIProvider("does-not-exist")


def test_unknown_state_raises() -> None:
    with pytest.raises(FixtureError, match="no state"):
        FixtureUIProvider("notes", state="s-nope")


def test_load_by_path_uses_sibling_dir(tmp_path: Path) -> None:
    (tmp_path / "mini.json").write_text(json.dumps(_mini()), encoding="utf-8")
    p = FixtureUIProvider(tmp_path / "mini.json")
    assert p.snapshot().snapshot_id == "s-0"
    assert p.list_apps() == ["Mini"]  # no apps.json: fixture apps only


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("cmd+n", "cmd+n"),
        ("Command+Shift+N", "shift+cmd+n"),
        ("cmd+shift+n", "shift+cmd+n"),
        ("alt+cmd+L", "opt+cmd+l"),
        ("option+command+l", "opt+cmd+l"),
        ("ctrl+shift+tab", "ctrl+shift+tab"),
        ("cmd+ctrl+alt+shift+x", "ctrl+opt+shift+cmd+x"),
        ("Enter", "return"),
        ("esc", "escape"),
        ("cmd+backspace", "cmd+delete"),
        ("cmd++", "cmd++"),
        (" cmd + w ", "cmd+w"),
        ("shift", "shift"),
    ],
)
def test_normalize_keys(raw: str, want: str) -> None:
    assert normalize_keys(raw) == want


# ---------------------------------------------------------------------------
# Provider + executor behaviour
# ---------------------------------------------------------------------------


def test_satisfies_protocols() -> None:
    p, x = _exec()
    assert isinstance(p, UIProvider)
    assert isinstance(x, ExecutorProtocol)


def test_menu_transition_and_log() -> None:
    p, x = _exec("notes")
    r = x.run("s-notes-0", Action.menu(110))
    assert (r.ok, r.changed, p.state) == (True, True, "s-notes-1")
    assert r.snapshot is not None and r.snapshot.snapshot_id == "s-notes-1"
    assert x.log == [Action.menu(110)]


def test_keys_fire_the_matching_menu_shortcut() -> None:
    p, x = _exec("finder")
    r = x.run("s-finder-0", Action.key("alt+cmd+L"))
    assert r.ok and r.changed and p.state == "s-finder-1"


def test_keys_without_menu_or_transition_stay_put() -> None:
    p, x = _exec("notes", "s-notes-1")
    r = x.run("s-notes-1", Action.key("return"))
    assert (r.ok, r.changed, p.state) == (True, True, "s-notes-1")


def test_keys_for_disabled_menu_do_nothing() -> None:
    p, x = _exec("finder")  # File > Move to Trash (cmd+delete) is disabled without a selection
    r = x.run("s-finder-0", Action.key("cmd+delete"))
    assert (r.ok, r.changed, p.state) == (True, False, "s-finder-0")


def test_scripted_keys_transition() -> None:
    p, x = _exec("textedit", "s-textedit-1")
    assert x.run("s-textedit-1", Action.key("escape")).changed
    assert p.state == "s-textedit-2"


def test_press_missing_or_disabled_fails() -> None:
    p, x = _exec("finder")
    missing = x.run("s-finder-0", Action.press(999))
    assert not missing.ok and "999" in (missing.error or "")
    disabled = x.run("s-finder-0", Action.menu(115))  # File > Open, nothing selected
    assert not disabled.ok and "disabled" in (disabled.error or "")
    assert p.state == "s-finder-0"


def test_menu_op_on_element_fails_but_press_on_menu_works() -> None:
    p, x = _exec("notes")
    assert not x.run("s-notes-0", Action.menu(1)).ok
    assert x.run("s-notes-0", Action.press(110)).changed
    assert p.state == "s-notes-1"


def test_item_without_transition_is_ok_unchanged() -> None:
    p, x = _exec("notes")
    r = x.run("s-notes-0", Action.menu(133))  # Edit > Copy
    assert (r.ok, r.changed, p.state) == (True, False, "s-notes-0")


def test_type_semantics() -> None:
    p, x = _exec("notes", "s-notes-1")
    assert x.run("s-notes-1", Action.type_text("standup")).changed
    assert x.run("s-notes-1", Action.type_text("standup", target=30)).ok
    assert not x.run("s-notes-1", Action.type_text("x", target=999)).ok
    assert p.state == "s-notes-1"


def test_scroll_and_wait() -> None:
    _, x = _exec("safari")
    assert x.run("s-safari-0", Action.scroll("down")).changed
    w = x.run("s-safari-0", Action.wait(10))
    assert w.ok and not w.changed


def test_open_app_switches_fixture_and_remembers_state() -> None:
    p, x = _exec("finder")
    x.run("s-finder-0", Action.menu(156))
    r = x.run("s-finder-1", Action.open_app("notes"))
    assert r.ok and r.changed and p.snapshot().app.name == "Notes" and p.state == "s-notes-0"
    assert x.run("s-notes-0", Action.open_app("Finder")).changed
    assert p.state == "s-finder-1"


def test_open_app_same_unknown_and_synthetic() -> None:
    p, x = _exec("finder")
    same = x.run("s-finder-0", Action.open_app("Finder"))
    assert same.ok and not same.changed
    assert not x.run("s-finder-0", Action.open_app("Photoshop 2031")).ok
    syn = x.run("s-finder-0", Action.open_app("Spotify.app"))
    assert syn.ok and syn.changed
    snap = p.snapshot()
    assert snap.app.name == "Spotify" and _menu(snap, "Spotify", "Quit Spotify")[1] == "cmd+q"


def test_stale_snapshot_from_another_app_fails() -> None:
    _, x = _exec("finder")
    x.run("s-finder-0", Action.open_app("Notes"))
    r = x.run("s-finder-0", Action.menu(110))  # id planned on Finder's snapshot
    assert not r.ok and "stale" in (r.error or "")
    assert x.run("s-finder-0", Action.key("cmd+n")).ok  # target-less actions are fine


def test_undo_pops_history_per_app() -> None:
    p, x = _exec("notes")
    assert x.undo() is False
    x.run("s-notes-0", Action.menu(110))
    assert x.undo() is True and p.state == "s-notes-0"
    x.run("s-notes-0", Action.menu(137))
    assert x.run("s-notes-3", Action.key("cmd+z")).changed  # Edit > Undo via its shortcut
    assert p.state == "s-notes-0" and x.undos == 2


def test_reset_returns_to_start() -> None:
    p, x = _exec("finder")
    x.run("s-finder-0", Action.open_app("Notes"))
    p.reset()
    assert p.snapshot().snapshot_id == "s-finder-0"


def test_synthetic_fixture_shape() -> None:
    fx = synthetic_fixture("zoom.us")
    assert validate_fixture(fx) == []
    assert fx.initial == "s-zoom-us-0"


def test_default_dir_points_at_eval_fixtures() -> None:
    assert DEFAULT_FIXTURES_DIR.name == "fixtures" and (DEFAULT_FIXTURES_DIR / "apps.json").exists()


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------


def test_untargeted_type_with_nothing_focused_changes_nothing() -> None:
    _, x = _exec("safari")  # s-safari-0 has no focused item
    r = x.run("s-safari-0", Action.type_text("cornell"))
    assert r.ok and not r.changed


def test_undo_label_variants_pop(tmp_path: Path) -> None:
    d = _mini()
    snaps = d["snapshots"]
    assert isinstance(snaps, dict)
    undo = {"id": 50, "kind": "menu", "path": ["Edit", "Undo Typing"], "shortcut": "cmd+z"}
    for k in ("s-0", "s-1"):
        snaps[k] = {**snaps[k], "items": [*snaps[k]["items"], undo]}
    (tmp_path / "mini.json").write_text(json.dumps(d), encoding="utf-8")
    p = FixtureUIProvider(tmp_path / "mini.json")
    x = FakeExecutor(p)
    x.run("s-0", Action.press(1))
    assert x.run("s-1", Action.menu(50)).changed and p.state == "s-0"


def test_transition_with_unknown_field_is_rejected() -> None:
    bad = [{"from": "s-0", "action": {"op": "press", "targt": 1}, "to": "s-1"}]
    with pytest.raises(FixtureError, match="targt"):
        Fixture.from_dict(_mini(transitions=bad))


def test_press_transition_on_menu_item_fires(tmp_path: Path) -> None:
    d = _mini(transitions=[{"from": "s-0", "action": {"op": "press", "target": 60}, "to": "s-1"}])
    snaps = d["snapshots"]
    assert isinstance(snaps, dict)
    item = {"id": 60, "kind": "menu", "path": ["File", "Go"], "shortcut": None}
    for k in ("s-0", "s-1"):
        snaps[k] = {**snaps[k], "items": [*snaps[k]["items"], item]}
    (tmp_path / "mini.json").write_text(json.dumps(d), encoding="utf-8")
    p = FixtureUIProvider(tmp_path / "mini.json")
    assert FakeExecutor(p).run("s-0", Action.press(60)).changed and p.state == "s-1"


def test_safari_address_bar_focus() -> None:
    p, x = _exec("safari")
    x.run("s-safari-0", Action.key("cmd+l"))
    assert p.snapshot().focused_id == 4


def test_quit_brings_previous_app_forward_and_forgets_state() -> None:
    p, x = _exec("finder")
    x.run("s-finder-0", Action.open_app("Notes"))
    x.run("s-notes-0", Action.menu(110))
    x.run("s-notes-1", Action.open_app("Slack"))
    assert x.run("s-slack-0", Action.key("cmd+q")).changed  # Slack > Quit Slack via its shortcut
    assert p.snapshot().app.name == "Notes" and p.state == "s-notes-1"
    x.run("s-notes-1", Action.menu(104))  # Quit Notes
    assert p.snapshot().app.name == "Finder"
    x.run("s-finder-0", Action.open_app("Notes"))
    assert p.state == "s-notes-0"  # relaunched fresh


def test_quitting_the_only_app_leaves_finder() -> None:
    p, x = _exec("slack")
    x.run("s-slack-0", Action.menu(104))
    assert p.snapshot().snapshot_id == "s-finder-0"


def test_empty_trash_shows_confirmation_dialog() -> None:
    p, x = _exec("finder")
    x.run("s-finder-0", Action.menu(102))
    assert p.state == "s-finder-5" and p.snapshot().focused_id == 41
    x.run("s-finder-5", Action.key("return"))
    assert p.state == "s-finder-0"


def test_paste_changes_focused_text_only() -> None:
    _, x = _exec("notes", "s-notes-1")  # body focused
    assert x.run("s-notes-1", Action.menu(134)).changed
    _, y = _exec("safari")  # nothing focused
    assert not y.run("s-safari-0", Action.menu(124)).changed
