# Flexibility of declaring types by string names ahead of time.
from __future__ import annotations

__all__ = [
    "Dependency",
    "PackageMetadata",
    "YoctoVersion",
    "__version__",
    "main",
    "render_recipe",
]

__version__ = "0.1.0"


def __dir__() -> list[str]:
    return __all__
