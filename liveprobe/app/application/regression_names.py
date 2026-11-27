"""Literal Node regression definitions shared by patch review and the host runner."""
import re


def node_test_definitions(text):
    return {match[1]: match[0] for match in re.finditer(
        r"\b(?:test|it)\(\s*['\"]([^'\"]+)['\"][\s\S]*?(?=\b(?:test|it)\(|\Z)", text,
    )}
