from sprint import Sprint, parse_sprints


SPEC_WITH_SPRINTS = """# Test App

## Vision
A test app.

## Features

### P0: Login
- Description: User login

### P1: Dashboard
- Description: Main dashboard

## Sprints

### Sprint 1: Foundation
Features: Login
Goal: Users can log in and see a landing page

### Sprint 2: Dashboard
Features: Dashboard
Goal: Logged-in users see a functional dashboard

### Sprint 3: Polish
Features: Animations, Error States
Goal: App feels polished with smooth transitions
"""

SPEC_WITHOUT_SPRINTS = """# Test App

## Vision
A test app.

## Features

### P0: Login
- Description: User login
"""


def test_parse_sprints_extracts_all():
    sprints = parse_sprints(SPEC_WITH_SPRINTS)
    assert len(sprints) == 3
    assert sprints[0].number == 1
    assert sprints[0].name == "Foundation"
    assert sprints[0].features == ["Login"]
    assert "log in" in sprints[0].goal
    assert sprints[1].number == 2
    assert sprints[1].name == "Dashboard"
    assert sprints[2].number == 3
    assert sprints[2].features == ["Animations", "Error States"]


def test_parse_sprints_fallback_single():
    sprints = parse_sprints(SPEC_WITHOUT_SPRINTS)
    assert len(sprints) == 1
    assert sprints[0].number == 1
    assert sprints[0].name == "Full Build"
    assert sprints[0].features == []
    assert sprints[0].goal == ""


def test_sprint_dataclass():
    s = Sprint(number=1, name="Core", features=["Auth", "DB"], goal="Basic auth works")
    assert s.number == 1
    assert s.name == "Core"
    assert s.features == ["Auth", "DB"]
    assert s.goal == "Basic auth works"
