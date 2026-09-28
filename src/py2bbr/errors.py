"""
py2bbr exception types

Every expected failure is expressed as a subclass of `Py2BbrError` so the
CLI can turn them into clean, non-traceback error messages.
"""


class Py2BbrError(Exception):
    """Base class for all py2bbr errors."""

    def __str__(self) -> str:
        return super().__str__()


class ConfigurationError(Py2BbrError):
    """The command-line or environment configuration is invalid."""


class YoctoVersionError(ConfigurationError):
    """An unknown or unresolvable ``--yocto-version`` was supplied."""


class TargetRuntimeError(ConfigurationError):
    """An unparsable ``--python-version`` / ``--target-arch`` was supplied."""


class MetadataError(Py2BbrError):
    """The package metadata could not be fetched or parsed."""


class MetadataNotFoundError(MetadataError):
    """The package (or version) could not be found on any configured index."""


class RecipeError(Py2BbrError):
    """A recipe could not be rendered from the available metadata."""


class LicenseError(RecipeError):
    """No usable license could be determined for the recipe."""


class CheckError(Py2BbrError):
    """A validation stage failed."""


class ToolNotFoundError(CheckError):
    """A required external tool (bitbake / yocto-check-layer / kas) is missing."""


class SubprocessError(CheckError):
    """An external command exited non-zero or could not be executed."""

    def __init__(
        self, command: list[str], returncode: int, detail: str = ""
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.detail = detail
        super().__init__(
            f"command failed (exit {returncode}): {' '.join(command)}"
            + (f" :: {detail}" if detail else "")
        )
