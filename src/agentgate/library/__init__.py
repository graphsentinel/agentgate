"""AgentGate declared-contract library — the AgenticArchitecture spec → DeclaredContract.

The statistical drift core (baseline, z-score, n-gram, scoring) lives in DriftWatch, not here —
AgentGate only declares + generates + governs at the creation boundary; runtime tool-call drift is
DriftWatch's job. The two share the contract *format* (a protocol), not code.
"""
from .contract import AgentContract, DeclaredContract, build_contract, resolve_instructions

__all__ = ["AgentContract", "DeclaredContract", "build_contract", "resolve_instructions"]
