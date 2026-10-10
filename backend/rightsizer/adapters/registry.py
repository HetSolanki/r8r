"""A small, lazy registry of adapter classes.

Built-in adapters are listed by import path and imported the first time they
are asked for, so registering one costs nothing at start-up and cannot create
an import cycle with the engine that uses it. Another installed package adds
its own through an entry point -- in its ``pyproject.toml``::

    [project.entry-points."rightsizer.clouds"]
    gcp = "rightsizer_gcp:GCPProvider"

-- or at runtime with :meth:`Registry.register`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from importlib.metadata import entry_points
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str, group: str, builtin: dict[str, str]) -> None:
        self.kind = kind
        self.group = group
        #: name -> "module:Class" or an already-loaded class. Order is priority:
        #: detection tries adapters in this order.
        self._entries: dict[str, str | type[T]] = dict(builtin)
        self._plugins_loaded = False
        #: Bumped whenever the set of adapters changes, so callers can cache.
        self.version = 0

    def register(self, name: str, target: str | type[T]) -> None:
        """Add or replace an adapter. ``target`` is a class or "module:Class"."""
        self._entries[name] = target
        self.version += 1

    def _load_plugins(self) -> None:
        if self._plugins_loaded:
            return
        self._plugins_loaded = True
        for ep in entry_points(group=self.group):
            # A built-in name is never shadowed by a plugin by accident.
            if ep.name in self._entries:
                log.warning("%s plugin %r ignored: the name is already registered", self.kind, ep.name)
                continue
            self._entries[ep.name] = ep.value
            self.version += 1

    def names(self) -> list[str]:
        self._load_plugins()
        return list(self._entries)

    def get(self, name: str) -> type[T]:
        self._load_plugins()
        if name not in self._entries:
            raise KeyError(f"unknown {self.kind} {name!r}; known: {', '.join(self._entries)}")
        target = self._entries[name]
        if isinstance(target, str):
            module, _, attr = target.partition(":")
            target = getattr(importlib.import_module(module), attr)
            self._entries[name] = target
        return target

    def __iter__(self) -> Iterator[type[T]]:
        for name in self.names():
            yield self.get(name)
