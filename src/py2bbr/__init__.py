from importlib.metadata import version

__all__ = [
    "Dependency",
    "PackageMetadata",
    "YoctoVersion",
    "__version__",
    "main",
    "render_recipe",
]

__version__ = version("py2bbr")


def __dir__() -> list[str]:
    return __all__
