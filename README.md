<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/triberoi/baraza/main/assets/logo-on-dark.svg">
  <img src="https://raw.githubusercontent.com/triberoi/baraza/main/assets/logo.svg" alt="baraza" width="240">
</picture>

Attendance analytics for Luma calendars, on your own machine.

Point it at a folder of Luma guest-list exports, or at the Luma API with your own key,
and it tells you who comes back: repeat attendance, first-timers, lapsed regulars, and
cohort retention. It runs locally, in your browser. There is no account and no server.

## The name

A **baraza** (Swahili, ba-RAH-za) is the stone bench built into the front of a house
along the Swahili Coast: Stone Town, Mombasa Old Town, Lamu, Malindi. It is also the
public gathering that happens on it, where people sit, meet, talk, play bao, drink
coffee, and receive guests on the street side of the house.

The word names both the bench and the people who habitually gather on it. That is what
this tool measures: who comes back, how often, and how that changes.

Swahili is the official language of Kenya and Tanzania. We use the word for its meaning,
and we have tried to describe it accurately. If you speak Swahili and we have it wrong,
open an issue and we will fix the text.

## Install

```bash
pip install baraza
```

Python 3.11 or newer.

## Use

Any Luma account can export a guest list from an event page: no paid plan, no API key.
Download a few into a folder, then:

```bash
baraza import ~/Downloads/luma-exports
baraza serve
```

`serve` prints a link and opens it. The link carries a one-time key for that session; if
the browser does not open, paste the link yourself.

**Export a guest list after its event has ended, and re-import if you exported early.** A
no-show is worked out from the guest list, so an export taken while the event is still
running does not yet know who failed to turn up. Everyone who has not checked in is still
"registered". baraza reads what the export knew, so importing that early snapshot and never
re-importing leaves the event looking like a 100% turnout. Export it again once the event
is over and import that.

**Someone who checked in attended, whatever they said beforehand.** A guest who declined
and then turned up on the night counts as having attended.

**If your event requires approval, people you never admitted are not counted as absent.**
Someone who asked to come and never got a decision had no place to give up, so they are
not a no-show and they are not in your turnout. They are demand, and they are counted as
that instead: how many applied, how many are still waiting on a decision, and how many
declined or withdrew.

That last number cannot be split. Luma uses one word both for an organizer turning an
application down and for a guest saying they are not coming, so nothing can tell those
apart. The app says so beside the number.

**If an event took no attendance, say so, and its people still count.** Some events never
get a check-in: a livestream watched somewhere else, or a door nobody had time to scan.
Left alone, everyone who said yes to one of those looks like a no-show. Open Settings,
find the event, and set it to "No attendance was taken". The people who said yes then count
as having attended, and carry into returning and the lifecycle labels.

Those events show no turnout figure, and they are left out of the calendar-wide one. An
event nobody counted has no turnout to report, and scoring it 100% would flatter the event
you did not check while making the ones you did look worse. Wherever a figure includes one
of these, the screen says so: the event is marked, and the rate says how many events it
covered.

baraza never works this out for itself. An event nobody scanned and an event nobody came
to arrive as the same rows, so a guess would turn the second into the first.

**A first import often reports events that need a name.** A guest-list export carries the
guests and an event id, but not the event's title or date, and Luma will only tell us
those for a public event. So a private one arrives like this:

```
0 events, 0 people, 0 registrations.

1 events need a name and a date before their guests can be used:
  evt-mBc2X4  38 guests  Founders Coffee
```

Nothing has to be downloaded twice, but **keep the folder**: the guests live in those CSVs
until the event has a name. Open the app, fill in the name and date on the Import screen,
and read the folder again. That second read brings the guests in.

With a paid Luma plan you can skip the exports:

```bash
baraza import --luma --key secret-your-key-here
```

The key is checked with Luma before it is saved, so a typo cannot replace a key that
works.

A key on the command line is also in your shell history, and visible to anyone who can
list processes on the machine. If that matters where you are, set `LUMA_API_KEY` in the
environment instead. baraza reads it and does not write it to disk.

Two more commands: `baraza report` prints a summary for piping, and `baraza mcp` runs a
read-only MCP server so an AI assistant can ask about your community. The MCP server
needs an optional extra: `pip install 'baraza[mcp]'`.

**What the assistant sees.** The MCP tools answer with your attendance figures and your
attendees' **names**. They never send email addresses: a guest your export did not name
comes back without one, never with the local part of their address. Those names are other
people's personal data, going to whichever AI service you have connected. Event titles and
guest names are also text other people wrote, so an assistant reading them is reading input
you did not author.

## Where your data lives, and what is in it

Your guest lists stay on your machine. Nothing about your attendees is uploaded, and
there is nothing to sign in to.

One thing does leave, and only on the folder path: a guest-list export carries an event
id but no event name or date, so baraza asks Luma's public event endpoint what that id
is called. Only the event id goes out: no guest, no name, no email address, no API key. To
ask nobody anything, `baraza import --offline <folder>` skips the lookup, and you fill in
event names and dates yourself on the Import screen.

`baraza import` creates a single SQLite file, `baraza.db` in the current directory
unless you pass `--store`. Two more files sit beside it:

| File | Holds | Appears when |
|---|---|---|
| `baraza.db` | your events, and **your attendees' names and email addresses** | you first import |
| `baraza.db.token` | the key this session's browser link carries | you first run `serve` |
| `baraza.db.luma-key` | your Luma API key | you first pass `--key` |

The first is other people's personal data, held by you. The other two are credentials.
All three are worth keeping somewhere only you can read.

If you have shared a `serve` link by accident (a screenshot, a pasted terminal), delete
`baraza.db.token`. The running server stops accepting the old key immediately, and the
next `serve` makes a new one.

**On macOS and Linux** the two credential files are created readable by your user only
(`0600`), and baraza re-applies that every time it opens them.

**On Windows it cannot do that.** Windows file permissions are access-control lists, not
mode bits, and the call baraza uses on other platforms has no effect there. The files
inherit the permissions of the folder they are in. In a personal folder that is normally
fine; **in a shared folder your Luma API key is readable by anyone who can read that
folder.** If you share the machine, keep your store somewhere only your account can open.

## The local server

`baraza serve` binds to `127.0.0.1` only, and there is no option to change that. There is
no login, because nothing outside your machine can reach the socket. Requests are also
checked for where they came from, so a web page you happen to have open cannot use the
server behind your back.

To reach it from another machine, put a reverse proxy in front of it.

## Not affiliated with Luma

Baraza is not affiliated with or endorsed by Luma. It reads a calendar you own, through
Luma's API or your own CSV exports, with your own credentials.

## Contributions

This repository is generated. Issues are welcome; pull requests are not accepted here,
because the code is exported from an upstream tree rather than edited in place.

`DEVELOPING.md` describes how the package is put together and how it is tested, for
anyone reading the source.

## License

Apache-2.0. Built and maintained by TribeROI Inc.
