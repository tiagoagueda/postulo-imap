"""postulo-imap: read the job-hunt mailbox and suggest what it says happened.

Every application produces mail — an acknowledgement, a rejection, an invitation with a
date, an assessment link, eventually an offer — and every one of those has to be typed
into the timeline by hand, which is why timelines go stale. This plugin reads **one
folder that the person chose**, matches each message to an application, decides what kind
of message it is from rule lists that anyone can read and translate, and files a
**suggestion**. Nothing is written into the record on a guess: a person accepts or
declines each one, and neither answer is asked for twice.

IMAP, not POP3, deliberately: POP3 has no folders, no flags and no server-side state, so
a job tracker near a mailbox could only download and usually delete. IMAP reads one
folder, marks what it has processed, and leaves everything else untouched.

Every string a person reads is translated by this package itself, from ``locale/`` next to
this file; the rule lists live in ``rules/`` as data, so a language is added without
writing code.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    #: What Postulo shows beside the connection: the version that was *installed*, read from
    #: the package's own metadata rather than typed here. Two numbers, one in
    #: `pyproject.toml` and one in the source, drift the first time only one of them is
    #: raised, and the one people read is the wrong one.
    __version__ = version("postulo-imap")
except PackageNotFoundError:  # pragma: no cover - running from a checkout, uninstalled
    __version__ = "0.0.0"
