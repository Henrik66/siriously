# Brain build plan (coordination doc)

Spec: `PRD.md` (§4 contracts, §5 requirements). Contracts are DONE in `brain/contracts.py`; import from there, never redefine shapes.
Settings: `brain/config.py` (`Settings`, env vars `SIRIOUSLY_*`). Tooling: `uv sync`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run mypy brain eval`.
Branch: `feat/brain`. **Do not commit or push**; the coordinator (session `siriously-f6`) commits after the quality gate.

## Rules for every worker
- Python 3.11+, stdlib + `rapidfuzz`, `pyyaml`, `fastapi`, `uvicorn`, `websockets` only. Need another dep? Ask the coordinator.
- mypy strict clean, ruff clean, no `Any` leaking from public functions, type hints everywhere.
- Tests co-located under `tests/test_<module>.py`. Every module ships tests. Must run on Windows/Linux/macOS.
- **Only edit the files you own** (table below). If you need a change in someone else's file or in `contracts.py`, message the coordinator.
- Keep functions < ~60 lines. No network at runtime except the local Bonsai server.

## Ownership

| Workstream | Owner | Files |
|---|---|---|
| W0 contracts, config, integration, commits | coordinator `siriously-f6` | `brain/contracts.py`, `brain/config.py`, `pyproject.toml`, `docs/` |
| W1 fixtures, fakes, eval harness | peer `siriously-57` | `brain/fixtures.py`, `eval/**`, `tests/test_fixtures.py`, `tests/test_eval.py` |
| W2 Bonsai server (Windows) + planner | peer `siriously-ed` | `scripts/bonsai/**`, `brain/planner/**`, `tests/test_planner*.py` |
| W3 router: control + fast path | subagent | `brain/control.py`, `brain/fastpath.py`, `brain/router.py`, `tests/test_control.py`, `tests/test_fastpath.py`, `tests/test_router.py` |
| W4 execution loop, safety, flow memory | subagent | `brain/pipeline.py`, `brain/safety.py`, `brain/flows.py`, `tests/test_pipeline.py`, `tests/test_safety.py`, `tests/test_flows.py` |
| W5 event bus + HTTP/WS server + CLI | subagent | `brain/events.py`, `brain/server.py`, `brain/cli.py`, `tests/test_server.py`, `tests/test_events.py` |

## Pinned interfaces (code against these even before they exist)

```python
# brain/control.py (W3)                       PRD §5.1 step 1, regex only, < 50 ms
def parse_control(text: str) -> ControlWord | None   # stop|cancel|undo|yes|no ("yes", "confirm", "do it" -> "yes")

# brain/fastpath.py (W3)                      PRD §5.2, no model, <= 30 ms
class FastPath:
    def __init__(self, apps: Sequence[str]) -> None: ...
    def match(self, text: str, snapshot: UISnapshot) -> Plan | None: ...

# brain/flows.py (W4)                         PRD §5.5
def parse_teach(text: str) -> str | None             # "call that standup" -> "standup"
@dataclass(frozen=True)
class FlowStep:                                       # an Action with its target as a descriptor
    action: Action                                    # target field = original id (informational only)
    target: TargetDescriptor | None
@dataclass(frozen=True)
class Flow:
    name: str
    steps: tuple[FlowStep, ...]
    utterance: str = ""                               # original command, used as planner hint on fallback
class FlowStore:
    def __init__(self, path: Path | None = None) -> None: ...   # None = in-memory (tests/eval)
    def names(self) -> list[str]: ...
    def match(self, text: str) -> str | None: ...     # exact or fuzzy flow-name match ("standup", "run standup")
    def get(self, name: str) -> Flow | None: ...
    def save(self, flow: Flow) -> None: ...
def resolve_step(step: FlowStep, snapshot: UISnapshot) -> Action | None   # None = descriptor didn't resolve

# brain/router.py (W3)                        PRD §5.1 order: control -> teach -> flow -> fastpath -> planner
class Router:
    def __init__(self, fastpath: FastPath, flows: FlowStore, planner: Planner | None) -> None: ...
    def route(self, text: str, snapshot: UISnapshot) -> Route: ...
    # Route(kind="control", control=...) | Route(kind="flow", teach=True, flow=name)  ("call that X")
    # | Route(kind="flow", flow=name) (replay; plan=None, pipeline resolves stepwise)
    # | Route(kind="fastpath", plan=...) | Route(kind="planner"|"dictation", plan=...) | Route(kind="none", reason=...)
    # Route.ms = decision time. planner=None -> Route(kind="none", reason="no planner").

# brain/planner/bonsai.py (W2)                PRD §5.3
class BonsaiPlanner:                                  # satisfies contracts.Planner
    def __init__(self, apps: Sequence[str] = (), settings: Settings | None = None) -> None: ...
    def plan(self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None) -> Plan: ...
    last_stats: dict[str, float]                      # prompt_tokens, completion_tokens, ms, cached_tokens
# brain/planner/rank.py:  def rank_items(text: str, snapshot: UISnapshot, k: int = 15) -> list[UIItem]
# Raises PlannerError (brain/planner/errors.py) on server down / timeout / unparseable output. Never invents ids.

# brain/safety.py (W4)                        PRD §5.6
DANGER_RE: re.Pattern[str]                            # send|delete|remove|quit|erase|empty trash|sign out
def needs_confirm(plan: Plan, snapshot: UISnapshot) -> bool   # target label/menu path, plus cmd+q / cmd+delete keys

# brain/pipeline.py (W4)                      PRD §5.4 + §5.6 + §5.5
class Brain:
    def __init__(self, provider: UIProvider, executor: Executor, router: Router,
                 planner: Planner | None, flows: FlowStore, sink: EventSink,
                 settings: Settings | None = None) -> None: ...
    def handle(self, utt: Utterance) -> Route: ...    # blocking; thread-safe w.r.t. stop() from another thread
    def stop(self) -> None: ...                       # aborts the current burst between actions
def build_brain(provider: UIProvider, executor: Executor, sink: EventSink,
                settings: Settings | None = None, planner: Planner | None = None) -> Brain

# brain/events.py (W5)
class EventBus:                                       # EventSink; thread-safe emit(); async subscribe() for WS clients
    def emit(self, event: Event) -> None: ...
    def subscribe(self) -> AsyncIterator[Event]: ...  # or queue-based, W5's call
    history: list[Event]                              # last N events for late joiners / debugging

# brain/fixtures.py (W1)
class FixtureUIProvider:                              # UIProvider over a fixture file; tracks current state
    def __init__(self, fixture: str | Path, fixtures_dir: Path | None = None) -> None: ...
class FakeExecutor:                                   # Executor applying scripted transitions to a FixtureUIProvider
    def __init__(self, provider: FixtureUIProvider) -> None: ...
    log: list[Action]
def load_apps(fixtures_dir: Path | None = None) -> list[str]   # eval/fixtures/apps.json
```

## Fixture file format (W1 owns; others may rely on it)
`eval/fixtures/<name>.json`:
```json
{"name": "notes", "initial": "s-notes-0",
 "snapshots": {"s-notes-0": {UISnapshot}, "s-notes-1": {UISnapshot}},
 "transitions": [{"from": "s-notes-0", "action": {"op": "menu", "target": 101}, "to": "s-notes-1"}]}
```
A `type` / `keys` action without a matching transition is `ok=true, changed=true` and stays on the same snapshot
(text entry). A `press`/`menu` on an id not in the current snapshot is `ok=false`. `open_app` switches to the
fixture whose `snapshots[initial].app.name` matches, if loaded. Ids are stable within a fixture.
Apps to cover (PRD §9 H+3): Finder, Notes, System Settings, Calendar, Slack (+ TextEdit, Safari optional).
Henrik will replace these with real captures from `MacUIProvider` later; keep the format identical to §4.2.

## Event semantics (W4 emits, W5 transports)
`heard` {text, speaker} → `route` {kind, reason, ms} → `plan` {plan, source} → `confirm` {plan, reason} (if needed)
→ `action` {i, action, ok, changed, error} per action → `done` {route, n_actions, ms} | `error` {message}.
Unverified speaker: only `rejected` {text, score}. `ms_since_speech_end` = now - utterance.t_speech_end (0 if t_speech_end==0).
