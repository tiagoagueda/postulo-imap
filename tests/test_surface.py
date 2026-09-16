"""This plugin imports `postulo.plugins.api`, or a line somebody wrote on purpose.

Postulo runs this check over the plugins it ships (`tests/test_plugin_surface.py` there,
postulo/postulo#126). A plugin in its own repository is where it matters more, not less:
nothing outside the core can be fixed by the person who moves a core module, so an import
that reaches past the surface is how this plugin breaks between two Postulo releases with
nobody warned.

The check reads the source rather than importing it, so a lazy `from postulo.core import
…` inside a method counts exactly as much as one at the top of a file — it is the same
dependency, spelled more quietly.

**`REACHING_PAST` is not a list of exemptions.** Every entry is a name that ought to be on
the surface and is not yet, and says which; adding one is a decision somebody makes on
purpose. A stale entry fails too, because one nobody removed hides the next real
dependency.

Only `src/` is measured. The tests are not shipped and are entitled to reach into Postulo
to set a world up, which is a different promise from the one the plugin makes at run time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "postulo_imap"

#: The one module a plugin may import from, and the reason there is exactly one.
SURFACE = "postulo.plugins.api"

#: What this plugin still reaches past the surface for, and why. Each of these is named in
#: postulo/postulo#229; the day the surface carries them, the import changes and the line
#: below goes.
REACHING_PAST: dict[str, str] = {
    "postulo.applications.models": (
        "`Application`, which is what a message is matched *to*, and `Suggestion`, which is "
        "how an earlier answer identifies the thread a reply belongs to. A plugin that files "
        "suggestions against applications cannot avoid naming them (#229)"
    ),
    "postulo.applications.suggestions": (
        "`suggest`: filing one, keyed on the message's own identifier so neither answer is "
        "asked for twice. Writing rows directly instead would be worse, not more independent "
        "(#229)"
    ),
    "postulo.jobs.models": (
        "`Contact`: the addresses a person recorded, which is the second-strongest signal "
        "for which application a message is about (#229)"
    ),
}


def sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def postulo_imports(path: Path) -> set[str]:
    """Every `postulo.*` module this file imports, at any depth, read rather than run."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if not node.level else []
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found |= {name for name in names if name == "postulo" or name.startswith("postulo.")}
    return found


def reaching() -> set[str]:
    found: set[str] = set()
    for path in sources():
        found |= postulo_imports(path)
    return found


def test_there_is_something_to_check():
    """A sanity check on the check: an empty package would make everything below pass."""
    assert len(sources()) >= 4


def test_the_plugin_imports_the_surface_or_something_written_down():
    past = sorted(reaching() - {SURFACE} - set(REACHING_PAST))

    assert not past, (
        f"postulo-imap reaches past the plugin surface: {past}. Either the name belongs in "
        f"postulo.plugins.api — ask for it there — or record the dependency in REACHING_PAST "
        f"with the reason it is needed. A new one is a decision, not an accident."
    )


@pytest.mark.parametrize("module", sorted(REACHING_PAST))
def test_nothing_recorded_has_quietly_been_fixed(module: str):
    """A stale entry would let a real dependency back in behind it."""
    assert module in reaching(), (
        f"postulo-imap no longer imports {module}. Delete that line from REACHING_PAST: the "
        f"list is the map of what postulo/postulo#229 still has to move, and a stale entry "
        f"hides the next one."
    )


def test_the_surface_is_what_this_plugin_actually_uses():
    """The other half: the surface is imported, rather than merely not reached past."""
    assert SURFACE in reaching()
