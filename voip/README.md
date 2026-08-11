# VoIP: virtual PBX and the AddPac gateway

Asterisk runs natively on this Mac and talks to an AddPac VoiceFinder AP1100F
gateway, which drives up to 8 analogue handsets over its FXS ports.

## Wiring

    Mac (en6) 192.168.100.2 ──ethernet── 192.168.100.3 AddPac AP1100F ──RJ11── handsets

The AddPac has two ethernet interfaces. Its LAN side (10.1.1.1, DHCP server)
serves only a stripped-down web page for IP settings, and **refuses telnet**.
The WAN side accepts telnet and carries SIP, so the Mac is cabled to it with a
static address.

`en6` loses its static address on reboot, falling back to a self-assigned
169.254.x.x. Asterisk then fails to start with `Can't assign requested
address`, because `pjsip.conf` binds to 192.168.100.2. Restore it with:

    sudo ipconfig set en6 MANUAL 192.168.100.2 255.255.255.0

`./scripts/run-asterisk.sh` checks for this before starting and says so.

## Why not Docker

Docker Desktop on macOS runs containers inside a Linux VM, which has no access
to `en6`. Packets to the gateway left with the home Wi-Fi source address
instead of 192.168.100.2 and were never answered, and packets arriving from the
gateway had their source rewritten by Docker NAT, so Asterisk could not match
them to an endpoint. Neither is fixable with port publishing — the container
needs to be on the 192.168.100.0/24 segment, and under Docker Desktop it cannot
be. `docker-compose.yml` and `Dockerfile` are left in place for reference but
**must not be used**: they will not reach the gateway and will take UDP 5060
away from the working PBX.

## Building

Homebrew has no `asterisk` formula, so it is built from source into
`asterisk-local/` (git-ignored, ~350 MB with the source tree):

    ./scripts/build-asterisk.sh

The script applies two macOS fixes: the hardcoded pjproject library suffix in
`main/Makefile`, and disabling `res_geolocation`, which embeds XML using GNU
linker syntax Apple's `ld` rejects.

## Running the PBX

    ./scripts/run-asterisk.sh            # foreground, verbose
    ./scripts/run-asterisk.sh -d         # detached
    ./scripts/run-asterisk.sh -r         # attach a console
    ./scripts/run-asterisk.sh -x "..."   # one CLI command

Configs live in `etc/` and are copied into the install tree on every start, so
`etc/` stays the only place to edit them.

## Extensions

| Number  | What it does                                    |
|---------|-------------------------------------------------|
| 101-108 | FXS ports 0/0-0/3 and 1/0-1/3, in that order     |
| 500     | plays a short message, then hangs up             |
| 600     | echo test — speech is played back to the caller  |
| 700     | answers and holds the line open, for digit reading |

Ring a handset from the host:

    ./scripts/run-asterisk.sh -x "channel originate PJSIP/105@addpac application Echo"

## Web panel

The game server serves a control panel for the phones at **`/voip`**. It shows
the whole chain as separate indicators — en6's static address, AMI, the SIP
trunk's qualify state, and the gateway's telnet CLI — because each fails on its
own and for its own reason, and a red tile names the command that fixes it.

Below that, one tile per FXS port: the extension, its slot, whether the port is
idle or in a call, the digits being dialled on it right now, and buttons to
ring it or drop the call. Dialled digits also stream into a log, over the same
AMI events `scripts/phone_digits.py` reads, so both can run at once. The
gateway's read-only CLI commands are buttons at the bottom.

Backed by `app/voip.py` and served from `app/server.py`, so it comes up with
the game (`./start.sh`) — no extra process. It works with Asterisk stopped;
the page just shows which link is down.

Ringing a handset needs the `originate` AMI write class, which is why
`etc/manager.conf` grants more than `system,call`.

## Reading dialled digits

`scripts/phone_digits.py` prints every digit dialled on a handset, reading
Asterisk's AMI event stream:

    python3 scripts/phone_digits.py            # one line per digit
    python3 scripts/phone_digits.py --group    # buffer into whole numbers
    python3 scripts/phone_digits.py --post http://localhost:5000/api/phone

Output names the FXS port the digit came from:

    [14:20:21] 107 (1/2) dialled 907

Digits arrive by two routes. A number dialled *before* the call is set up is
collected by the gateway and shows up as the extension in its INVITE. Keys
pressed *during* a call arrive as DTMF, one event each.

The second route is the one that matters for a rotary phone behind a
pulse-to-DTMF converter: the converter needs an open line to signal into, so
the call has to exist first. Every port's PLAR already points at extension 700,
which answers and then holds the line open. Lifting the handset opens a channel
straight away, with no number to dial first, and every digit becomes a line of
output.

`pjsip.conf` sets `dtmf_mode=auto`, so digits are read whether the gateway
relays them out-of-band as RFC 2833 or passes the converter's tones through as
audio.

This firmware has **no pulse dialing support** — neither `voice-port` nor
`dial-peer pots` offers a dial-type setting, so the FXS ports decode DTMF and
nothing else. A rotary dial only works through a converter.

## Gateway CLI

`scripts/addpac.py` runs commands over telnet (login `root` / `router`) and
handles the pager, so output comes back whole:

    python3 scripts/addpac.py "show voice port summary" "show sip"

Useful commands:

| Command                     | Shows                                     |
|-----------------------------|-------------------------------------------|
| `show voice port summary`   | per-port state — Idle, Busy               |
| `show voice port 1/0`       | full settings for one port                |
| `show call active`          | calls in progress                         |
| `show sip`                  | SIP server and registration state         |
| `show running-config`       | the whole configuration                   |
| `write`, then `y`           | save to flash — changes are lost otherwise|

## Configuring the gateway

`scripts/addpac.py` is read-only. `scripts/addpac_config.py` is the write side:

    python3 scripts/addpac_config.py            # print what it would send
    python3 scripts/addpac_config.py --apply    # apply, then save to flash

Re-run it after a factory reset, or whenever a handset starts refusing the
second call. It is idempotent. The save to flash matters: without it every
change is lost on the next power cycle, which is how the settings below went
missing once already.

What it sets:

- `no forced-clear-down` on all eight ports. **This is the fix for a handset
  that takes one call and then answers everything with 503.** The factory
  setting, `forced-clear-down -55 60`, ends a call only after sixty continuous
  seconds below -55 dB. Until that timer expires the port sits in
  `Disconnecting` and refuses new calls — and with a converter or an off-hook
  handset holding the loop closed, the timer never expires at all. Asterisk
  signals the end of a call over SIP; the gateway does not need to infer it
  from silence.
- `timing fxs-powerdown-duration 1`, down from 2. The port drops its line feed
  for this long after a call, and the panel can place the next one about three
  seconds later.
- `connection plar 700` on all eight voice ports.
- `no register`, dropping the failing registration loop (see below). The
  negation takes no argument — `no register e164` is rejected.
- `no ip-share enable`. NAT between the WAN and LAN sides does nothing on a
  direct cable and only rewrites the voice path.
- SIP server at `192.168.100.2`, replacing the dead provider address
  `64.148.237.145`.

Numbers 101-108 are mapped onto the eight FXS ports by `dial-peer voice 1-8
pots`, which the script leaves alone.

The gateway offers one set of provider credentials
(`partner4@permngn.usi.ru`) for all eight numbers, and Asterisk has no matching
accounts, so registration reported `Failed` and retried every twenty seconds.
It is switched off rather than fixed: calls run over a static IP trunk in both
directions and never needed it. `show sip` still prints the old registration
table until the gateway is power-cycled; `show running-config` is what says
whether the setting actually went.

`busyout monitor gatekeeper` and `busyout monitor voip-interface` are not
supported by firmware 8.30U — the CLI rejects them either way.

## Calling a handset that is off-hook

A port that is not `Idle` answers an INVITE with 503, and the panel shows
`Circuit/channel congestion`. An off-hook handset puts its port there and keeps
it there: PLAR dials 700 the moment the handset lifts, 700 answers and holds
the line open, and the port stays `Busy` for as long as the handset is off the
cradle.

Clearing the port does not help, and this is worth spelling out because it
looks like it should. Hanging up and bouncing the port does return it to
`Idle` — but PLAR re-seizes it about **five seconds** later, because the
handset is still off-hook and nothing has changed. That is less time than it
takes to bounce the port and get an INVITE back to it, so the call lands in a
port that is `Busy` again and is refused, which is exactly the failure the
bounce was meant to prevent.

So `originate()` in `app/voip.py` does not fight PLAR — it uses the call PLAR
already made. If a channel exists for this extension, sitting on 700 and
answered, it is moved to the target with an AMI `Redirect`. Nothing is torn
down, there is no window to lose, and the handset — already at someone's ear —
hears the music straight away instead of being rung.

An on-hook handset has no such channel, so it is dialled with `Originate` as
before and the phone rings. The port can still be `Disconnecting` from a call
the gateway has not finished tearing down; that case is bounced first via
`reset_port()`, which polls `show voice port summary` until the port reads
`Idle` — `no shutdown` returns before the port is usable again.

## Known hardware fault

A handset on port 1/0 **rings** when the PBX calls 105, but lifting it never
registers: the port stays `Idle`, Asterisk never sees the call answered, and
the gateway keeps sending ring bursts.

Ringing and answering use different circuits. The ringer is coupled across the
line and works no matter what the hookswitch does; answering requires the
handset to close the loop and draw current. So the ring path — cable, both
conductors, and the FXS port itself — is proven good, and the fault is that
the handset does not draw enough loop current for the port to detect.

Gateway-side causes are ruled out: the port reports dial tone generation
enabled, no busyout, no tie connection, and this firmware exposes no loop
current or impedance settings.
