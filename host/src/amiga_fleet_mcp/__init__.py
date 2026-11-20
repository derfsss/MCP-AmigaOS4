"""amiga-fleet-mcp — MCP server for AmigaOS 4 fleets."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    """The repository's VERSION file, else the installed package's version.

    VERSION at the repository root is the one declaration of the version:
    MCPd's build, the release script and this package's metadata
    (hatchling's [tool.hatch.version]) all read it. A source checkout reads
    it directly, so an editable install never reports a version that
    predates a bump; an installed wheel has no repository around it and
    answers from the metadata the file was built into.
    """
    root = Path(__file__).resolve().parents[3]
    if (root / "host" / "pyproject.toml").is_file() and (root / "VERSION").is_file():
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        return version("amiga-fleet-mcp")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version()
__all__ = ["__version__"]
