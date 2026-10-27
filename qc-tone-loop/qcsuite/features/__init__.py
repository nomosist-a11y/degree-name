"""The features. Each module follows one contract so the CLI and web UI stay uniform.

A feature module ``qcsuite/features/<name>.py`` provides:

* plain functions doing the work, taking a :class:`~qcsuite.device.core.Device`
  or :class:`~qcsuite.device.session.Rig` and returning JSON-able dicts - the
  CLI and the web routes both call these, never each other;
* ``add_cli(sub)`` registering its ``qcsuite <command>`` subparsers, whose
  handlers take ``(ctx, args)`` (see :mod:`qcsuite.cliutil`);
* optionally a web router in ``qcsuite/web/routes/<name>.py`` and a page
  ``qcsuite/web/static/<name>.html`` + ``<name>.js``.

Rules every feature keeps:

* stored presets change only through :mod:`qcsuite.plan` (dry run first);
* anything that reamps warns about monitor volume first;
* renders whose audio is the DI coming back are excluded
  (:mod:`qcsuite.audio.passthrough`) together with blocks known not to sound
  (:mod:`qcsuite.device.inert`);
* long work is a :mod:`qcsuite.jobs` job, resumable and logged to JSONL;
* nothing here judges which sound is better - results are shown, sorted and
  recorded, and the player decides.
"""

#: Module names, in the order the CLI lists them.
MODULES = ["dilib", "library", "backup", "leveler", "tournament", "panel", "sweep",
           "sheets", "setlist", "batch", "health", "watch", "remote"]
