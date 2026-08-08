"""fwd — move local coding work and agent sessions to remote compute.

The package is layered deliberately so that each layer can be developed and tested in isolation:

- ``sshexec``      : the only place that shells out to ``ssh``; everything remote goes through an ``SSHEndpoint``.
- ``config``/``state`` : pure data layers (TOML config, ``~/.fwd/state.json``) with no side effects beyond file IO.
- ``backends/*``   : provisioning strategies behind the ``Provisioner`` protocol; they return a ``TargetInfo`` and know nothing about Claude.
- ``sync``/``remote`` : mechanical project and remote-environment setup.
- ``agents/*``     : class-based coding-agent integrations and their state-transfer helpers.
- ``ops/*``        : orchestration of the above into user-facing operations.
- ``cli``          : thin Typer surface that only parses flags and delegates to ``ops``.
"""

from importlib.metadata import PackageNotFoundError, version


try:
    # The distribution is named fwdit because the fwd name is owned by an unrelated PyPI project; the import package and command intentionally remain fwd.
    __version__ = version("fwdit")
except PackageNotFoundError:
    # Keep source-tree imports useful before the project has been installed, while installed builds always report their tag-derived package metadata.
    __version__ = "0.0.0"
