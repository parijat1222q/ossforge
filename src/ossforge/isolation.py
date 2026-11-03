"""Optional Linux verification process containment, not assistant isolation.

Prepared runtimes are explicit read-only mounts. No dependency installation or
network access happens inside this boundary. The caller still trusts host git.
"""
from __future__ import annotations

import sys
import shutil
from pathlib import Path


class Unavailable(ValueError):
    pass


def wrap(command: list[str], clone: Path, mode: str, readonly_paths: list[str]) -> list[str]:
    if not isinstance(mode, str) or mode not in {'none', 'bwrap'}:
        raise Unavailable('verification_isolation must be none or bwrap')
    if mode == 'none':
        return command
    if sys.platform != 'linux':
        raise Unavailable('Bubblewrap isolation and offline environment replay require Linux; no host fallback')
    binary = shutil.which('bwrap')
    if not binary:
        raise Unavailable('requested verification isolation unavailable: bubblewrap is not installed')
    root = clone.resolve(strict=True)
    if not isinstance(readonly_paths, list) or any(not isinstance(p, str) for p in readonly_paths):
        raise Unavailable('verification_readonly_paths must be a list of absolute paths')
    mounts = []
    for raw in readonly_paths:
        path = Path(raw)
        if not path.is_absolute() or not path.exists() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise Unavailable('verification mount must be an existing absolute non-symlink path')
        resolved = path.resolve(strict=True)
        if resolved in {Path('/'), Path('/home'), Path('/root'), Path('/tmp'), Path('/etc'), Path('/run'), Path('/proc'), Path('/dev'), Path.home()}:
            raise Unavailable('verification mount is too broad or conflicts with private runtime paths')
        if resolved == root or root.is_relative_to(resolved) or resolved.is_relative_to(root):
            raise Unavailable('verification runtime mounts must be separate from the source checkout')
        mounts.append(resolved)
    args = [binary, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
    for name in ('/usr', '/bin', '/lib', '/lib64', '/etc/alternatives', '/etc/ld.so.cache'):
        if Path(name).exists():
            args += ['--ro-bind', name, name]
    args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/home',
             '--dir', '/tmp/home']
    for path in mounts:
        args += ['--ro-bind', str(path), str(path)]
    args += ['--ro-bind', str(root), str(root), '--chdir', str(root), '--clearenv',
             '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/tmp/home',
             '--setenv', 'TMPDIR', '/tmp', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
             '--setenv', 'PYTHONNOUSERSITE', '1', '--setenv', 'CI', '1',
             '--setenv', 'LANG', 'C.UTF-8', '--', *command]
    return args
