"""E13 creation-driven validation — the declared graph must be a scope-monotonic DAG (§B, §1).

Run at **generate time**, not in `build_contract`: E11/E12 contracts predate these invariants and
need not satisfy them (a contract may exist purely for the per-call/sequence declared-check). But to
*generate a runnable app*, the delegation graph must be acyclic and scope-monotonic — a cyclic graph
or a hand-off that widens scope **fails to generate**, so the running app cannot contain those
violations (creation-driven governance).
"""
from __future__ import annotations

from ..library.contract import DeclaredContract


def _scope_subset(child: frozenset[str], parent: frozenset[str]) -> bool:
    """True if `child` scope ⊆ `parent` scope (delegation must not widen scope).

    Empty `parent` = unconstrained → any child is fine. Empty `child` under a constrained parent is a
    *widening* (child would be unconstrained) → not a subset. Otherwise every child prefix must sit
    under some parent prefix (equal or nested).
    """
    if not parent:
        return True
    if not child:
        return False
    return all(any(cs == ps or cs.startswith(ps + "/") for ps in parent) for cs in child)


def find_cycle(contract: DeclaredContract) -> list[str] | None:
    """Return a cycle in the delegation graph as a node path (`a -> b -> a`), or None if acyclic.

    DFS with white/gray/black colouring; a back-edge to a gray node is a cycle. Self-edges (`a -> a`)
    are reported too. Deterministic (sorted traversal) so the reported cycle is stable.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(contract.agents, WHITE)
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GRAY
        stack.append(n)
        for m in sorted(contract.agents[n].can_delegate_to):
            if m not in contract.agents:
                continue  # edge to unknown agent is caught by build_contract; ignore here
            if color[m] == GRAY:
                return stack[stack.index(m):] + [m]
            if color[m] == WHITE:
                found = visit(m)
                if found:
                    return found
        color[n] = BLACK
        stack.pop()
        return None

    for n in sorted(contract.agents):
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def topological_order(contract: DeclaredContract) -> list[str]:
    """A deterministic topological order of the agents (delegators before delegatees).

    Requires an acyclic graph (call `validate_for_generation` first). Used by targets that run a
    linear pipeline (e.g. CrewAI sequential): the coordinator/roots come first, leaves last.
    """
    import bisect

    indeg = dict.fromkeys(contract.agents, 0)
    for a in contract.agents.values():
        for d in a.can_delegate_to:
            if d in indeg:
                indeg[d] += 1
    queue = sorted(n for n, deg in indeg.items() if deg == 0)
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in sorted(contract.agents[n].can_delegate_to):
            if m in indeg:
                indeg[m] -= 1
                if indeg[m] == 0:
                    bisect.insort(queue, m)
    return order


def validate_for_generation(contract: DeclaredContract) -> None:
    """Raise ValueError unless the contract can be generated: acyclic + scope-monotonic.

    Called by the generators before emitting any code, so a forbidden topology never reaches output.
    """
    cycle = find_cycle(contract)
    if cycle:
        raise ValueError(f"delegation graph has a cycle: {' -> '.join(cycle)} (must be a DAG)")
    for src in sorted(contract.agents):
        a = contract.agents[src]
        for dst in sorted(a.can_delegate_to):
            d = contract.agents.get(dst)
            if d is not None and not _scope_subset(d.scope, a.scope):
                raise ValueError(
                    f"scope escalation on delegation {src} -> {dst}: "
                    f"{sorted(d.scope)} is not a subset of {sorted(a.scope)}"
                )
