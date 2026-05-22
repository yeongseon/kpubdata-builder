"""Silver stage normalization: apply only spec-declared transforms.

Per the Silver stage principle, the Builder must not invent its own notion
of "clean" data. Normalization applies only transforms explicitly declared
in the BuildSpec. Anything not declared is left untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl


def normalize_table(table: pl.DataFrame, transforms: Sequence[str] = ()) -> pl.DataFrame:
    """Apply spec-declared transforms to a table.

    With no declared transforms (the common case), the table is returned
    unchanged. Declared transforms that the Builder does not yet support
    raise an error rather than being silently ignored, to keep behavior
    deterministic and avoid implying transforms ran when they did not.
    """
    if not transforms:
        return table
    unsupported = ", ".join(repr(t) for t in transforms)
    raise ValueError(
        f"Unsupported transforms declared in spec: {unsupported}. "
        "The Silver stage does not yet implement declarative transforms."
    )
