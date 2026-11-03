"""Public companion guides carried by the installable distribution."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

NAMES = ('AGENTS.md', 'docs/local-ai.md', 'ossforge.example.yaml')


def read(name: str) -> str:
    if name not in NAMES:
        raise ValueError('unknown public guide')
    resource = files('ossforge').joinpath('public_guides', name)
    if resource.is_file():
        return resource.read_text(encoding='utf-8')
    # Editable/source installs already carry the same public companion files.
    source = Path(__file__).resolve().parents[2]
    if not (source / 'pyproject.toml').is_file() or not (source / 'AGENTS.md').is_file():
        raise ValueError('public guides missing from this distribution')
    return (source / name).read_text(encoding='utf-8')


def export(destination: str) -> list[str]:
    """Create a NEW companion directory. Never replace an existing file/tree."""
    root = Path(destination).absolute()
    if '..' in root.parts or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('guide destination must not contain symlinks or traversal')
    # Read every packaged source before creating the destination.
    sources = {name: read(name) for name in NAMES}
    root.mkdir()  # exclusive new directory; parent must already exist
    # Preserve a partial new tree on I/O failure for inspection, never delete it.
    for name, text in sources.items():
        target = root / name
        target.parent.mkdir(exist_ok=True)
        with target.open('x', encoding='utf-8', newline='\n') as out:
            out.write(text)
    return [str(root / name) for name in NAMES]
