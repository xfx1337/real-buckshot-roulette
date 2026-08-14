// Wiring and network settings for the TA-1132 dial reader.
//
// Everything here is a compile-time constant. Change a value, reflash.

#pragma once

// ── network ────────────────────────────────────────────────────────────
//
// The Wi-Fi network, the server's address and the token are not written
// here. ./flash.sh asks for them once and writes them to config.local.h,
// which is included below and is not worth committing — it holds a Wi-Fi
// password.
//
// That file exists by the time anything is compiled, because flash.sh
// creates it before it builds. Compiling by hand without it is what the
// fallbacks under the include are for: they let the file compile, and the
// firmware then says on the serial console that it has nothing to connect
// to, rather than silently trying to reach an address from an example.
#if __has_include("config.local.h")
#include "config.local.h"
#endif

#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif

// The machine running the game server, as the ESP has to address it —
// 127.0.0.1 is the server's own loopback and means nothing here, so the
// server also has to be bound to 0.0.0.0 for anything but the machine
// itself to reach it.
//
// Port 8000, because the telephony moved into the game server: it used to be
// a separate Flask process on 8080 (scripts/web.py), and the game answers
// /api/dialer itself now. The path is unchanged and the old port still works
// wherever that Flask process is still running.
#ifndef SERVER_HOST
#define SERVER_HOST ""
#endif
#ifndef SERVER_PORT
#define SERVER_PORT 8000
#endif
#define SERVER_PATH "/api/dialer"

// Which handset this ESP speaks for. The server maps it onto an FXS port,
// so it has to be one of 101..108.
#ifndef EXTENSION
#define EXTENSION "101"
#endif

// A shared secret sent as the X-Dialer-Token header. The server rejects a
// request without it when it has one set. Empty sends no header.
#ifndef DIALER_TOKEN
#define DIALER_TOKEN ""
#endif

// ── pins ───────────────────────────────────────────────────────────────
//
// Both contacts are dry: one side to the pin, the other to GND. The pins
// are held up by their internal pull-ups, so closed reads LOW.
//
// Avoid GPIO 6-11 (SPI flash), GPIO 34-39 (input-only, no pull-up), and
// GPIO 0/2/12/15 (strapping pins — a contact closed at reset changes how
// the chip boots). 14 and 26 are clear of all of that.

// НИ, the impulse contact. It opens and closes once per unit of the digit
// dialled while the disc returns.
//
// Marked D14 on most boards. It is also HSPI's clock and the bootloader
// pulses it while starting, which shows up on a scope but means nothing to
// a pin only ever read as an input.
#define PIN_PULSE 14

// The hook switch. Closed with the receiver lifted.
//
// Set to -1 when no hook is wired — the receiver is then assumed lifted and
// every digit counts. Leaving it on a pin with nothing attached is what
// blocks dialling outright: the pin floats HIGH, HIGH reads as a receiver
// down, and a receiver down discards every train.
//
// 26 means a contact is expected. Nothing wired to it floats HIGH, which
// reads as a receiver permanently down: dialling stops working and no hook
// event is ever sent. If the contact is not connected, put this back to -1.
#define PIN_HOOK 26

// The TA-1132's contacts, like every mechanical switch, do not change state
// cleanly: each transition is a burst of make-and-break lasting a few
// milliseconds. An edge is accepted only once the pin has held its new level
// for this long, so the burst is ridden out rather than counted.
//
// This is a settling time, not a blanking window: an edge counts only once
// the pin has held its new level for this long without changing again. The
// difference matters. A blanking window accepts the first edge of a bounce
// burst and then deafens itself, so the burst is counted and a real pulse
// arriving inside the window is lost — which is why the old value had to be
// pushed to 70 ms, close enough to the dial's own 107 ms to be fragile.
// Waiting for the level to settle rejects the whole burst instead, and needs
// only to outlast one burst rather than approach the pulse interval.
//
// 15 ms is what a working reader on this same telephone used.
#define DEBOUNCE_PULSE_MS 15

// The hook switch is a heavier contact than the dial's and settles far more
// slowly: at 50 ms it reported the receiver lifted eight times in a row for
// one lift. Nobody lifts and replaces a receiver inside a third of a second,
// so there is nothing real to lose by waiting.
#define DEBOUNCE_HOOK_MS 300

// ── dial timing ────────────────────────────────────────────────────────
//
// A digit is a train of pulses; the gap between two digits is the pause
// while the finger returns to the disc and finds the next hole. This
// threshold separates them: silence this long after the last pulse ends
// the digit.
//
// The gap inside a train is ~100 ms at the standard ten pulses per second,
// and a worn dial runs slower rather than faster — 8 pulses per second puts
// it at 125 ms. The pause between digits cannot be shorter than the time it
// takes to move a finger to the next hole and let the disc return, which is
// several hundred milliseconds at best.
//
// 400 ms sits between the two with room for a dial well outside spec. Too
// low is the failure that looks like a broken digit: a train split in the
// middle reports two digits instead of one, so the margin belongs on this
// side.
#define DIGIT_GAP_MS 400

// A dial that produced more pulses than this in one train is reporting a
// stuck or chattering contact, not a digit. 0 is ten pulses, so eleven or
// more cannot be real.
#define MAX_PULSES 11

// How many digits make a number. The extensions here are three digits, and
// completing on the third saves the caller waiting out a timeout.
#define NUMBER_LENGTH 3

// A part-dialled number this old is abandoned — the caller started dialling
// and stopped. It is discarded so the next digit starts a fresh number
// rather than joining one from minutes ago.
#define NUMBER_TIMEOUT_MS 10000

// ── outbound queue ─────────────────────────────────────────────────────
//
// Events are posted from a task of their own, so a slow or unreachable
// server never stalls pulse counting. This is how many can wait.
#define EVENT_QUEUE_DEPTH 16

// A POST that hangs holds the sender task; this bounds it.
#define HTTP_TIMEOUT_MS 4000

// Failed sends are retried this many times before the event is dropped.
#define HTTP_RETRIES 2
