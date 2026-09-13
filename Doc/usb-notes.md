# rfcat USB wedge — investigation notes (2026-09-13)

## Symptom

One run of `rf_reciv.py -D -t --sync` printed

    Error in resetup():USBTimeoutError(110, 'Operation timed out')

once a second for 45 s and received nothing. A `pyusb` `dev.reset()` (which
itself timed out after 30 s) brought the dongle back. Firmware is
`DONSDONGLE r5535` (Feb 2015), no CC-Bootloader.

## Reproduction

`tools/usb_stress.py cycles --count 200 --recv 2` reproduced it on the **second**
cycle, every time, in the pre-fix code:

    Error claiming usb interface:USBError(16, 'Resource busy')
    WARNING dongle opened but is not answering
    ERROR cycle 2: rfcat dongle is not responding …

Two processes racing (`race`) and killing a receiver mid-transfer (`kill`) hit
the same failure from the other direction.

## Cause

`rflib.chipcon_usb.USBDongle.cleanup()` — the only teardown the library offers,
and what the old `Radio.close()` called — just resets rflib's queues and
counters:

```python
def cleanup(self):
    self._usberrorcnt = 0
    self.recv_queue = b''
    ...
```

It does **not** release the claimed USB interface and does not stop the three
daemon threads (`run_ctrl`, `runEP5_recv`, `runEP5_send`) that keep issuing
bulk transfers on it. So:

1. Within one process, the next `RfCat()` cannot claim interface 0
   (`USBError(16, 'Resource busy')`), ends up with a handle that never answers,
   and `resetup()` — which retries forever, one line and one second per attempt
   — produces exactly the observed chatter.
2. Across processes, a receiver that exits without releasing (or is killed)
   leaves the interface claimed until the kernel tears the file descriptor
   down, so the *next run* sees the same thing. `timeout`/`kill` made it worse
   because `SIGTERM` skipped the `finally` blocks entirely.

The EP5 64-byte-transfer firmware bug (fixed upstream in 2017) was **not**
implicated — no `RFrecv` block of 59 bytes was involved, and the failure is
fully explained and fully fixed in software. The firmware was left alone.

## Fix

In `insteonrf/radio/rfcat.py`:

- `close()` → `_release()`: idle the radio, clear `_threadGo` (and
  `reset_event`), wait out the 10 ms/400 ms EP5 timeouts, then
  `releaseInterface()` + `finalize()` the pyusb handle, and only then
  `cleanup()`. A `_closed` flag makes `resetup()` a no-op afterwards so
  rflib's ctrl thread cannot re-claim the interface behind us.
- `resetup()` is overridden to be bounded (`RESETUP_TIMEOUT`, 8 s) and quiet
  (`_quiet = True`) instead of looping forever.
- Opening is health-checked (`getBuildInfo()` within 5 s, in a thread so a hung
  USB call cannot block the program); on failure it USB-resets once, waits 3 s
  and retries, then raises `DongleError` with what to do next.
- `receive()` watchdog: repeated timeouts *with* `_usberrorcnt` rising, a set
  `reset_event`, or a failed `resetup` trigger `heal()` — close, USB reset,
  reopen, restore the previous RX/TX configuration. `--no-auto-reset` disables.
- `usb_reset()` runs `dev.reset()` in a daemon thread and gives up after 10 s
  with "unplug and replug" rather than hanging.
- `cli.py` installs SIGINT/SIGTERM/SIGHUP handlers, and the receive loops check
  a `STOP` event after every block. Raising `KeyboardInterrupt` from the handler
  is *not* enough on its own, which cost a second round of debugging: rflib's
  `USBDongle.recv()` contains

      except KeyboardInterrupt:
          sys.excepthook(*sys.exc_info())
          break

  so it eats the exception, prints a traceback and carries on — a `kill` (or a
  Ctrl-C) left the receiver running and the interface claimed. `py-spy dump`
  on the surviving process showed the main thread back inside
  `recv()`/`recv_event.wait()`, which is what pointed at it. The CLI also
  installs an excepthook that drops `KeyboardInterrupt` tracebacks so rflib's
  traceback does not reach the terminal.

## Acceptance (final code, this host)

    python tools/usb_stress.py all      # 200 cycles + 50 kill + 20 race

- 200 open/configure/receive/close cycles: 0 failures (~15 s per 10 cycles).
- 50 kill-mid-receive cycles (SIGTERM at a random point, then immediate
  reopen): 0 failures.
- 20 two-process races: 0 failures. A race does leave the dongle needing one
  USB reset (the loser's claim), and the automatic reset recovered it every
  time — 15 resets over the run, all silent and successful.

Then, on the live radio: `insteon-rf recv -D -t` decoded PLM queries and device
ACKs generated through Home Assistant; `SIGTERM` stopped it within ~2 s and the
*next* process opened the dongle with no reset; a spoofed Ping was ACKed by
`29.4E.52`; and `insteon-rf monitor --mqtt` logged and published the folded
records.

No manual `insteon-rf reset` was needed at any point after the fix.
