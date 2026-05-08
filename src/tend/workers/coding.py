"""Deprecated shim: CodingWorker has been renamed to GeneralWorker.

Re-exported here for the migration window — this file is removed in
Task 12 once Brain stops importing from this path.
"""
from tend.workers.general import GeneralWorker as CodingWorker  # noqa: F401
