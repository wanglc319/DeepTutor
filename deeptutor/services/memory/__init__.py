"""Three-layer memory subsystem.

- ``backend`` : storage-layer abstraction — :class:`MemoryBackend` ABC,
                :class:`FileBackend` default impl, :class:`MemoryLock`
- ``trace``   : L1 raw event capture (append-only JSONL per surface per day)
- ``document``: L2/L3 markdown + footnote-citation parser/serializer (pure)
- ``ops``     : add/edit/delete batch applier (pure)
- ``paths``   : per-user path resolution
- ``ids``     : ULID-style trace and entry id generators
- ``store``   : public facade — :class:`MemoryStore` (delegates to backend)
- ``consolidator``: LLM-driven L1→L2 and L2→L3 ops

The v1 two-file `MemoryService` is gone. All callers go through
:func:`get_memory_store` and the :class:`MemoryStore` facade. Persistence
is pluggable via :class:`MemoryBackend` — add SQLite / PostgreSQL later
without touching callers above ``store``.
"""

from .backend import (
    FileBackend,
    MemoryBackend,
    MemoryLock,
)
from .ids import is_entry_id, is_trace_id, new_entry_id, new_trace_id
from .paths import L3_SLOTS, SURFACES, L3Slot, Surface, memory_path_service_override
from .store import (
    DocOverview,
    MemoryStore,
    get_memory_store,
    migrate_partner_surface_if_needed,
    migrate_v1_if_needed,
    reset_memory_store_for_testing,
)
from .trace import TraceEvent

__all__ = [
    "DocOverview",
    "FileBackend",
    "L3_SLOTS",
    "L3Slot",
    "MemoryBackend",
    "MemoryLock",
    "MemoryStore",
    "SURFACES",
    "Surface",
    "TraceEvent",
    "get_memory_store",
    "is_entry_id",
    "is_trace_id",
    "memory_path_service_override",
    "migrate_partner_surface_if_needed",
    "migrate_v1_if_needed",
    "new_entry_id",
    "new_trace_id",
    "reset_memory_store_for_testing",
]
