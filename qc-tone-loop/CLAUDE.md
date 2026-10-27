# Quad Cortex tone loop - instructions for Claude Code

You are running the tone-matching loop on the player's Mac, which has a Quad
Cortex connected over USB. You cannot hear audio. You judge every tone from
`compare.json`, `features.json` and the `compare.png` plot, together with what
the player tells you they hear.

Activate the venv first: `source .venv/bin/activate`.

## Before you start

1. **Cortex Control must be closed.** It holds the USB control link
   exclusively. If it's open, `qctone` fails with "Quad Cortex not found".
   Ask the player to quit it. They can reopen it after the session to see the
   result.
2. The player loads a starting preset on the unit, either one close to the
   target or a simple amp > cab chain. Edits are never saved unless you run
   `qctone save`, so the stored preset stays safe until then.
3. Run `qctone doctor --reamp-test`. It checks the USB control link, the
   firmware, the grid routing (is a row fed from USB 5 and reaching Out 1/2?),
   the USB audio device, and plays a short test note through the grid. Fix
   every FAIL before going on. The test note plays at -20 dBFS into the amp,
   so warn the player to turn monitors down first. If the reamp test is
   silent, the unit's USB channel map differs from the default. Set
   `QCTONE_REAMP_OUT` / `QCTONE_WET_IN` / `QCTONE_DRY_IN` (1-based channel
   numbers) and rerun.
4. The DI. Use `qctone init ... --di file.wav` with an existing dry recording,
   or run `qctone record-di NAME --seconds 20` while the player plays into
   Input 1. The DI should be the same part, in the same register, as the
   reference.

## Defining the target

    qctone init NAME --ref reference.wav --start 30 --dur 15 --desc "..."

- The reference should be **isolated guitar** if at all possible (a stem, an
  isolated track, or a section with no bass or drums). With a full mix, the
  bass and cymbals distort the low and high bands, so discount `sub`, `low` and
  `air` and say so to the player.
- `--desc` holds the player's words (for example "tight, mid-forward, not
  fizzy, 2000s metalcore rhythm"). Reread `target/description.md` on every
  iteration. Where the words and the numbers disagree, **the words win**.

## The loop

1. `qctone grid` reads the live grid: blocks, models and every parameter in
   real units, with its range.
2. Make sure the reamped row's input is `USB_5`. If not, route it with
   `qctone apply '{"ops":[{"op":"set_input","row":0,"port":"USB_5"}]}'`
   (`apply` changes the grid without rendering or creating an iteration). If
   the grid is empty, build a starting chain the same way, with `set_block`
   for drive, amp, cab and EQ. Point the input back at `INPUT_1` before
   saving if the player wants to play the preset live.
3. Write a patch JSON (format in `qctone/device.py`'s docstring) and run:

       qctone step NAME --patch /path/patch.json --note "what and why"

   This validates the patch (nothing is sent if any op is invalid), applies
   it, reamps the DI, analyzes the result, and prints the score and hints.
   Write patch files into the session folder or your scratchpad.
4. Look at `iter_NNN/compare.png` (Read the image) and `compare.json`. Plan
   the next patch from `mismatch_regions` and `hints`:
   - Fix structure first, then gain, then broad EQ, then fine EQ, then
     ambience. Choose amp, drive and cab/mic (`qctone catalog --category amp`,
     `--search`) before chasing small EQ moves.
   - Change **one idea per iteration**, at most 2-4 related knobs, so each
     result can be attributed.
   - `flatness_1k_5k` too high or crest too low means too much gain or
     compression. The reverse means too little.
   - Broad tilt usually comes from TREBLE/PRESENCE or cab/mic choice. Narrow
     peaks come from an EQ block. Add a Parametric EQ after the cab if none
     exists.
   - Keep the output level sensible. If `render_level.clipped` is true, lower
     the amp or lane level before anything else.
5. At about score 85+, or every 3-4 iterations, ask the player to listen (on
   the unit or by reamping in Logic Pro) and record their reaction with
   `qctone feedback NAME "too much low end on palm mutes"`. Their ears beat
   the score.
6. If an iteration made things worse, `qctone restore NAME --iter N` puts the
   grid back to iteration N, and `qctone restore NAME` puts it back to the
   baseline.
7. Stop when the player is happy, or when 3 iterations in a row fail to raise
   the best score. In that case, propose a structural change (different
   amp/cab) or ask the player for direction.
8. Save only on the player's explicit go-ahead:
   `qctone save NAME --slot 30A --title "..."` (this overwrites that User
   slot, so confirm the slot is free or unwanted).

## Logic Pro route (optional)

If the player prefers to reamp in Logic Pro, or bounces a mix there, analyze
that file instead:

    qctone grid --out /path/grid.json
    qctone import NAME bounced.wav --grid /path/grid.json --note "Logic bounce"

## Report each iteration to the player in one short block

Give the score (with the best so far), what you changed, what you'll change
next and why, and whether you want them to listen.

## Library caveats (pyquadcortex, unofficial)

- Verified on CorOS 4.0.1 only. If writes stop landing after a firmware
  update, say so rather than retrying blindly.
- A `set_block` the preset lacks DSP for is refused (`BlockRefused`). Use a
  lighter block, or remove one.
- Never call anything that changes global I/O settings. This tool only edits
  the grid.

# qcsuite - rules for Claude Code

`qcsuite` (in `qcsuite/`) is the everyday toolkit built on the same library:
preset management, backups, loudness levelling, blind comparisons, sweeps,
sheets, setlists/MIDI, batch reamps, health checks, an edit watcher. Its web
app is `qcsuite serve` (127.0.0.1 only). The user guide is
`docs/user-guide.md`; the hardware test plan is `docs/hardware-test-plan.md`.

## The principle

**The player judges the sound; you do the handling.** Render under identical
conditions, match loudness, hide names, sort, record, make changes undoable.
Never tell the player which tone is better, never pick a winner, never
"tune by numbers" with qcsuite. Feature distances and scores only order things
for the player to listen to.

## Before anything else

1. Until `docs/hardware-test-plan.md` has been run on this unit, every qcsuite
   feature is "verified on the mock only". Say so when you report results, and
   run the plan's first two checks (setlist limit, row output VOLUME audibility)
   before any write or any loudness levelling.
2. `--mock` runs against the fake unit (`qcsuite/device/fake.py`). Use it to
   rehearse a command before running it on the unit. Mock results say nothing
   about the unit's sound.
3. Cortex Control must be closed. If qcsuite says the device is in use, another
   qcsuite/qctone process holds `~/.qctone/device.lock`: ask, don't kill it.

## Safety rules (never relax them on your own)

- Writes go to the sandbox `_qcsuite_sandbox` by default. `My Presets`,
  `Favorite`, `kami`, `Cory Wong` and the Factory Library are refused by the
  code; do not look for a way around it. `Live`, `Artist`, `Song`, `Workshop`
  only with `--allow-folder NAME` when the player asked for that folder.
- Every write to stored presets is a plan: run it without `--apply` first,
  read the plan to the player, and add `--apply` only after they agree.
  Destructive steps back up first; read the backup path out to the player.
- Reading a stored preset RECALLS it: the unit's sound changes and unsaved
  edits on the unit are lost. Warn before backups, levelling, batch reamps and
  anything that loads presets one by one.
- Before anything that reamps, ask the player to turn monitors and headphones
  down.
- Never call whole-unit settings (I/O, Global EQ, master volume, firmware), and
  never answer the unit's capture dialog.
- Known non-effects from the computer (measured on this unit): capture
  `file_name` and the capture block's knobs, string parameters such as a cab's
  mic, unowned plugin blocks (Plini, Gojira, Nolly, Petrucci, John Mayer,
  Rabea, Misha, Tim Henson, Fortin, Soldano SLO-100 X). They read back fine and
  do not sound. Do not present results that depend on them as real; qcsuite
  flags and excludes such renders.
- Writes are not retried blindly: the unit answers most writes with silence.
  Confirm by reading back (`qcsuite library list`, `qcsuite status`).

## When something fails

Record it, stop, and ask. Do not edit the safety rules, the fake unit or the
tests to make a hardware result "pass". If the unit behaves differently from
the mock, the mock is wrong: write down what the unit did (numbers, messages)
for the player, and propose the change to `qcsuite/device/fake.py` separately.
