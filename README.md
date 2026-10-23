# postulo-imap

**Read one folder of your mailbox and let [Postulo](https://source.tiagoagueda.com/postulo/postulo)
suggest what it says happened: acknowledgements, rejections, interview invitations,
assessments, offers. Nothing is written into your record on a guess.**

Every application produces mail, and every piece of it has to be typed into the timeline
by hand, which is why timelines go stale. This plugin reads the folder you chose, works
out which application each message is about, decides what kind of message it is from
phrase lists anyone can read, and files a **suggestion**. You accept or decline each one;
neither answer is asked for twice.

**IMAP, not POP3**, deliberately. POP3 has no folders, no flags and no server-side state:
it downloads, and usually deletes. IMAP lets Postulo read one folder, mark what it has
read, and leave everything else alone — the only acceptable way to put a job tracker near
a mailbox.

## Install

Into the environment of a running Postulo:

```sh
uv pip install git+https://source.tiagoagueda.com/postulo/postulo-imap.git
```

Restart Postulo. In a container, bake it into the image — see *Plugins in the image* on
[Installing Postulo](https://source.tiagoagueda.com/postulo/postulo/wiki/Installing-Postulo).

## Use

**First make the folder.** In your mail client or on your server, create a folder — the
default name here is *Jobs* — and a rule that files recruiter mail into it. Postulo never
reads your inbox, and it reads nothing but this one folder: a connection that names `INBOX`
is refused. Every message it reads there is afterwards flagged, or moved, or (if you chose
to leave the mailbox untouched) left alone, whether or not the rules recognised it; a
message nothing recognised is skipped, with no suggestion, and flagged like the rest.

Then, under **Settings → Connections → Add a connection**, choose *Mailbox (IMAP)*:

| Field | What it is |
| --- | --- |
| Server, port, security | `imap.example.org`, 993 with TLS, or 143 with STARTTLS. Leave the port empty and it follows the security you chose. |
| Username, password | An app password wherever the server offers one. Gmail requires one with two-factor authentication on. Stored encrypted. |
| Folder to read | *Jobs*, or whatever you called it. |
| Once read | Flag with `$Postulo` (default, invisible in most clients), move to a subfolder, or leave the mailbox completely untouched. |
| Look back | How many days a pass considers, 1 to 3650. Thirty by default. |
| Run | Every 15 minutes to once a day, honoured by the scheduler; *Sync now* runs at once. |

Press *Test*: it signs in and says how many messages are waiting.

## What it does with a message

**Matching**, strongest evidence first: a reply to a message already matched; the sender's
address against a contact you recorded; the sender's domain against the company's website
(public mail providers prove nothing and are ignored, and `acme.co.uk` is told from every
other `.co.uk`); and last, the company name or the
role title in the subject or body. When two applications answer equally well — two roles
at one company — nothing is chosen, and you attach it by hand.

**Classifying**, from the phrase lists in `src/postulo_imap/rules/`: German, English,
Spanish, French, Italian, Dutch, Polish and Portuguese. Rules, not a model: they are
inspectable, translatable and wrong in ways you can see. The most consequential reading
wins, whatever language each phrase is in: offer, then rejection, then invitation, then
assessment, then acknowledgement, so a message that both thanks you and invites you is an
invitation, and a rejection that mentions an earlier interview is a rejection. Line wraps,
HTML tags and entities, non-breaking spaces and typographic apostrophes do not hide a phrase. An invitation's
dates are offered as the message wrote them, for you to put in the diary.

**A language with no phrase list is skipped, not guessed at.** Mail in one of the eight is
read; anything else matches nothing, no suggestion is filed, and your mailbox is left as it
was. Postulo's own interface speaks many more languages than this plugin can read mail in,
so if your rejections arrive in a language that is not listed, the list is what to add.

**Suggesting.** Every recognised message becomes a suggestion in
**Applications → Suggestions**: what it says, which application, who it is from, which
phrase was recognised, and — for a rejection, an offer, an invitation — the status it
would move the application to. Accepting writes it through Postulo's own services, so the
timeline says *imap* put it there and you can undo it by hand. Declining is remembered.

**What is stored**: the message's identifier, date, sender, subject and a short excerpt.
Not the body, beyond that excerpt, and no attachments.

**What is transferred**: a pass asks the server for 25 messages at a time, learns each one's
size first, and takes a message above a megabyte as its headers and the first 64 KiB of its
text only, so attachments are not downloaded and a folder's size does not set the memory a
pass needs. When you leave the mailbox untouched, a message already suggested is recognised
from its `Message-ID` header and not downloaded again. A message the plugin cannot read is
counted in the connection's notes, flagged with the rest, and does not stop the pass.

## Adding a language

Copy `src/postulo_imap/rules/en.json`, translate the phrases, name the file after the
language, add it to the set named in `tests/test_imap.py`, and open a pull request. No code
changes, and the tests will show you whether your patterns catch what they should.

The name is the language's BCP 47 tag as Postulo writes one, and the file says the same in
its `language`: `sv.json` for Swedish, or `pt-BR.json` if a variety needs phrases of its
own. A code is matched to a list the way tags are compared, without regard to case, and a
variety with no list of its own is read by its language's: `pt-BR` and `PT` both find
`pt.json`. The tests refuse a file named any other way.

Keep to words that belong to your language. The kinds are tried in order across all the
files, and within a kind the files are tried in turn, so a pattern that reads equally well as
English or Dutch will answer for those too, and the suggestion will name the wrong language.
Name the post in an offer ("pleased to offer you the *position*"): an offer outranks every
other kind, so a pattern that also fits an advert or an interview turns those into offers.

## Translations

The plugin's own words — the connection form, what a run reports, what a suggestion says —
are translated apart from the phrase lists, in `src/postulo_imap/locale/`. Postulo never translates a plugin's strings, and nobody translates this one in a commit:
every translation is made in Weblate, at
[translate.tiagoagueda.com](https://translate.tiagoagueda.com/projects/postulo/), which drafts
new strings by machine and sends what speakers save back as a pull request from its
`weblate` branch. A commit runs `uv run postulo-messages extract`, which leaves an empty slot
for a new string in every catalogue, and `uv run postulo-messages guard` in CI refuses one
that writes a translation (postulo/postulo#706). The `.mo` files are not committed:
`uv run postulo-messages compile` writes them, before the tests and before `uv build`.
`uv run pytest -m release` holds the languages the paired core release selects complete before
a release.

A language code here is a BCP 47 tag in its canonical form, as everywhere in Postulo: the
language in lower case, a script with a capital, a region in capitals (`fr-FR`, `pt-BR`,
`sr-Cyrl`). The directories under `locale/` are the one exception: `fr_FR` and `sr_Cyrl`
are gettext's names for a catalogue, not codes, and `postulo-messages` derives them from
the tags, so nobody types one.

## Develop

```sh
uv sync        # brings in Postulo itself from its repository, for the tests
uv run pytest
uv run ruff check .
```

The tests run against a mailbox that lives in memory — folders, keyword search, fetch,
flags, move — and inside a real Postulo through its registry and scheduler. No connection
leaves the machine.

## Licence

AGPL-3.0-or-later, like Postulo.
