"""Context optimization utilities."""

import re
from typing import List


def trim_context(content: str, max_lines: int = 15, include_timestamps: bool = False) -> str:
    """Trim context to essential error information.
    
    Args:
        content: Full content to trim.
        max_lines: Maximum number of lines to keep.
        include_timestamps: Whether to keep timestamps.
        
    Returns:
        Trimmed content.
    """
    lines = content.split('\n')
    
    # Remove timestamps if requested
    if not include_timestamps:
        lines = [strip_timestamp(line) for line in lines]
    
    # Find error-relevant lines
    relevant_lines = _extract_relevant_lines(lines, max_lines)
    
    return '\n'.join(relevant_lines)


    if not include_timestamps:
        lines = [strip_timestamp(line) for line in lines]
    
    # Find error-relevant lines
    relevant_lines = _extract_relevant_lines(lines, max_lines)
    
    return '\n'.join(relevant_lines)


def strip_timestamp(line: str) -> str:
    """Remove common timestamp formats from line.
    
    Args:
        line: Line to process.
        
    Returns:
        Line without timestamp.
    """
    # Common timestamp patterns
    patterns = [
        r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s+',  # 2026-02-12 13:45:32.123
        r'^\d{2}:\d{2}:\d{2}[.,]\d+\s+',  # 13:45:32.123
        r'^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s+',  # [2026-02-12 13:45:32]
        r'^\[\d{2}:\d{2}:\d{2}\]\s+',  # [13:45:32]
        r'^\d{10,13}\s+',  # Unix timestamp
    ]
    
    for pattern in patterns:
        line = re.sub(pattern, '', line)
    
    return line


def _extract_relevant_lines(lines: List[str], max_lines: int) -> List[str]:
    """Extract most relevant lines from content.
    
    Args:
        lines: All lines.
        max_lines: Maximum to return.
        
    Returns:
        Most relevant lines.
    """
    if len(lines) <= max_lines:
        return lines
    
    # Priority keywords for identifying important lines
    priority_keywords = [
        'error', 'exception', 'fatal', 'critical', 'failed',
        'traceback', 'stack trace', 'panic', 'warn',
        'at line', 'file "', '.py:', '.js:', '.java:'
    ]
    
    # Score each line
    scored_lines = []
    for i, line in enumerate(lines):
        score = 0
        line_lower = line.lower()
        
        # Higher score for lines with priority keywords
        for keyword in priority_keywords:
            if keyword in line_lower:
                score += 10
        
        # Prefer lines near the end (recent activity)
        recency_bonus = (i / len(lines)) * 5
        score += recency_bonus
        
        # Prefer non-empty lines
        if line.strip():
            score += 1
        
        scored_lines.append((score, i, line))
    
    # Sort by score and take top N
    scored_lines.sort(reverse=True, key=lambda x: x[0])
    top_lines = scored_lines[:max_lines]
    
    # Re-sort by original position to maintain order
    top_lines.sort(key=lambda x: x[1])
    
    return [line for _, _, line in top_lines]


def estimate_tokens(text: str) -> int:
    """Estimate token count for text.
    
    Rough approximation: 1 token ≈ 4 characters for English text.
    
    Args:
        text: Text to estimate.
        
    Returns:
        Estimated token count.
    """
    return len(text) // 4
