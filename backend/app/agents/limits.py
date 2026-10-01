"""Execution limits shared by fresh and resumed specialist graphs."""
import os


def graph_recursion_limit() -> int:
    limit = int(os.getenv('WDRUNGE_GRAPH_RECURSION_LIMIT', '1000'))
    if limit < 1:
        raise ValueError('WDRUNGE_GRAPH_RECURSION_LIMIT must be positive')
    return limit
