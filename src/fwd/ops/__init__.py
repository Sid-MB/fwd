"""Operations layer — orchestration of the mechanical modules into user-facing commands.

``cli.py`` contains no logic beyond flag parsing; every command body is one call into here. Keeping orchestration out
of the Typer layer means operations are directly callable and testable without a CLI runner, and the ordering of a
launch lives in exactly one readable place (:func:`fwd.ops.launch.launch`).

Submodules are imported lazily by ``cli.py`` inside each command body so that startup cost and import errors stay
scoped to the command actually being run.
"""

from __future__ import annotations
