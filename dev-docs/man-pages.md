# Manual pages

`fwd` maintains conventional section-1 manual pages under `man/`. [`click-man`](https://github.com/click-contrib/click-man) extracts names, synopses, descriptions, options, and visible subcommands from the same Typer declarations used by `fwd --help`. `tools/generate_man_pages.py` then adds the authored sections that command introspection cannot infer reliably: exit status, environment, files, examples, bug reporting, and cross-references.

Typer 0.27 uses its vendored Click implementation. The generator temporarily points `click-man` at those vendored classes so its context construction and option checks operate on the real `fwd` command tree. `click-man` remains a development dependency and does not increase the installed CLI's dependency surface.

## Generate and inspect

```sh
uv run python tools/generate_man_pages.py
uv run python tools/generate_man_pages.py --check
mandoc -T lint man/*.1
man ./man/fwd.1
man ./man/fwd-up.1
```

The source date is explicit and reproducible. Update `MANUAL_DATE` when intentionally revising the manual, regenerate all pages, and inspect both the source diff and rendered output. The generator removes obsolete `.1` files when a visible command is removed or renamed.

## Conventional structure

Every page contains `NAME`, `SYNOPSIS`, `DESCRIPTION`, and the applicable `COMMANDS` and `OPTIONS` sections generated from the CLI. The augmentation layer adds `EXIT STATUS`, `REPORTING BUGS`, and `SEE ALSO` to every page. The root `fwd(1)` page also documents `ENVIRONMENT`, `FILES`, and `EXAMPLES`.

Keep option and command behavior in `src/fwd/cli.py`; do not copy those inventories into the augmentation layer. Keep prose in the generator only when it describes behavior outside Click's model. Command-specific exit codes belong in `COMMAND_EXIT_STATUS`.

## Distribution

The source distribution includes `man/*.1` for Unix package maintainers. The wheel carries the same pages under `fwd/man`; editable installs read the repository-level `man/` directory. The console entry point synchronizes them silently on first use to `$XDG_DATA_HOME/man/man1`, or `~/.local/share/man/man1` by default. A versioned `.fwdit-man-pages.json` ownership manifest makes unchanged starts cheap, updates changed pages after an upgrade, and removes pages for deleted commands. It never uses `sudo`, writes a global man directory, or blocks the requested CLI command when the user data directory is unavailable. `fwd uninstall` removes the owned pages and manifest.

CI runs the generator in `--check` mode and lints every page with mandoc. The publish workflow repeats both validations before its release build, triggers for changes to the manuals or generator, and verifies that the resulting source distribution contains the root manual, a command manual, and the generator. Release and operating-system packaging consume only checked-in pages, so package builds do not need to import application code or execute a documentation generator.
