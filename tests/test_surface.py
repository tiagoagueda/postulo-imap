"""This plugin imports `postulo.plugins.api`, and nothing else from Postulo.

Postulo runs this check over the plugins it ships (`tests/test_plugin_surface.py` there,
postulo/postulo#126). A plugin in its own repository is where it matters more, not less:
nothing outside the core can be fixed by the person who moves a core module, so an import
that reaches past the surface is how this plugin breaks between two Postulo releases with
nobody warned.

**The check is imported now, not copied** (postulo/postulo#229). This file used to hold its
own AST walk and a `REACHING_PAST` table with three entries — `Application` and
`Suggestion`, `suggest`, and `Contact` — each a name that ought to be on the surface and was
not. All three are on it, the table is gone, and what is left is `postulo.plugins.testing`,
which the core maintains and every plugin can run.

Only `src/` is measured. The tests are not shipped and are entitled to reach into Postulo
to set a world up, which is a different promise from the one the plugin makes at run time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from postulo.plugins.testing import SURFACE, assert_imports_only_the_surface, imports_of

PACKAGE = "postulo_imap"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE


def test_there_is_something_to_check():
    """A sanity check on the check: an empty package would make everything below pass."""
    assert len(list(SOURCE.rglob("*.py"))) >= 4


def test_the_plugin_imports_only_the_surface():
    assert_imports_only_the_surface(SOURCE, package=PACKAGE)


def test_the_surface_is_what_this_plugin_actually_uses():
    """The other half: the surface is imported, rather than merely not reached past."""
    found = imports_of(SOURCE, package=PACKAGE)
    assert found, "the package imports nothing from Postulo"
    # By name since postulo/postulo 12504e44f: `postulo.plugins.api.FieldSpec`, not the module.
    assert all(name == SURFACE or name.startswith(SURFACE + ".") for name in found), found


@pytest.mark.parametrize(
    "name",
    [
        "Application",
        "Contact",
        "DestinationRefused",
        "Suggestion",
        "approve_host",
        "language_match",
        "suggest",
        "sync_state",
    ],
)
def test_every_name_this_plugin_needs_is_promised(name: str):
    """Not merely importable: on `__all__`, which is what the promise is made about.

    A name that resolves today because it happens to be reachable is not a name that keeps
    working across a release. Four of these were the whole of `REACHING_PAST`;
    `language_match` came with the rule files being matched as tags (#3).
    """
    from postulo.plugins import api

    assert name in api.__all__, name
