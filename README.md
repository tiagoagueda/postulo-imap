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
reads your inbox, and it reads nothing but this one folder.

Then, under **Settings → Connections → Add a connection**, choose *Mailbox (IMAP)*:

| Field | What it is |
| --- | --- |
| Server, port, security | `imap.example.org`, 993 with TLS, or 143 with STARTTLS. |
| Username, password | An app password wherever the server offers one. Gmail requires one with two-factor authentication on. Stored encrypted. |
| Folder to read | *Jobs*, or whatever you called it. |
| Once read | Flag with `$Postulo` (default, invisible in most clients), move to a subfolder, or leave the mailbox completely untouched. |
| Look back | How many days a pass considers. Thirty by default. |
| Run | Every 15 minutes to once a day, honoured by the scheduler; *Sync now* runs at once. |

Press *Test*: it signs in and says how many messages are waiting.

## What it does with a message

**Matching**, strongest evidence first: a reply to a message already matched; the sender's
address against a contact you recorded; the sender's domain against the company's website
(public mail providers prove nothing and are ignored); and last, the company name or the
role title in the subject or body. When two applications answer equally well — two roles
at one company — nothing is chosen, and you attach it by hand.

**Classifying**, from the phrase lists in `src/postulo_imap/rules/`, English, French and
Portuguese to start with. Rules, not a model: they are inspectable, translatable and wrong
in ways you can see. The most consequential reading wins, so a message that both thanks
you and invites you is an invitation. An invitation's dates are offered as the message
wrote them, for you to put in the diary.

**Suggesting.** Every recognised message becomes a suggestion in
**Applications → Suggestions**: what it says, which application, who it is from, which
phrase was recognised, and — for a rejection, an offer, an invitation — the status it
would move the application to. Accepting writes it through Postulo's own services, so the
timeline says *imap* put it there and you can undo it by hand. Declining is remembered.

**What is stored**: the message's identifier, date, sender, subject and a short excerpt.
Not the body, beyond that excerpt, and no attachments.

## Adding a language

Copy `src/postulo_imap/rules/en.json`, translate the phrases, name the file after the
language, and open a pull request. No code changes, and the tests will show you whether
your patterns catch what they should.

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
