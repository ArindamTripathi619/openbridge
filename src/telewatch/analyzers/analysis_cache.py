"""Analysis caching to reduce redundant LLM calls."""

import hashlib
import time
import re
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from collections import OrderedDict


@dataclass
class CachedAnalysis:
    """Cached analysis result with metadata."""
    analysis: Any  # Analysis object
    timestamp: float
    hit_count: int = 0


class AnalysisCache:
    """LRU cache for event analysis results."""
    
    def __init__(self, max_entries: int = 100, ttl_seconds: int = 3600):
        """Initialize cache.
        
        Args:
            max_entries: Maximum number of cached entries.
            ttl_seconds: Time-to-live for cache entries in seconds.
        """
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.cache: OrderedDict[str, CachedAnalysis] = OrderedDict()
        self.hits = 0
        self.misses = 0
    
    def _compute_signature(self, event) -> str:
        """Compute unique signature for an event.
        
        Args:
            event: Event object to hash.
            
        Returns:
            Unique hash signature.
        """
        # Hash based on: source, content (first 200 chars), and severity indicators
        content = getattr(event, 'content', '')
        source = getattr(event, 'source', '')
        
        # Strip timestamp to improve cache hit rate
        from .context_optimizer import strip_timestamp
        content = strip_timestamp(content)
        
        # Extract first 200 chars for signature (enough to identify error type)
        content_preview = content[:200].strip()
        
        # Create composite key
        signature_input = f"{source}:{content_preview}"
        
        # Hash it
        return hashlib.sha256(signature_input.encode()).hexdigest()[:16]

    def _compute_fuzzy_signature(self, event) -> str:
        """Compute a structural 'skeleton' signature for an event.
        
        Masks numbers, IDs, and common variable fragments to catch similar errors.
        
        Args:
            event: Event object to hash.
            
        Returns:
            Fuzzy hash signature.
        """
        content = getattr(event, 'content', '')
        source = getattr(event, 'source', '')
        
        # 1. Strip timestamp
        from .context_optimizer import strip_timestamp
        content = strip_timestamp(content)
        
        # 2. Skeletonize: Mask numbers and hex IDs
        # Replace numbers with '0'
        skeleton = re.sub(r'\d+', '0', content)
        # Replace hex items (e.g. memory addresses 0x7f...)
        skeleton = re.sub(r'0x[0-9a-fA-F]+', '0xX', skeleton)
        # Replace common ID-like strings (uuid-like)
        skeleton = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', skeleton)
        
        # 3. Take preview
        skeleton_preview = skeleton[:200].strip()
        
        # 4. Hash it with a 'fuzzy:' prefix to avoid exact collision
        signature_input = f"fuzzy:{source}:{skeleton_preview}"
        return hashlib.sha256(signature_input.encode()).hexdigest()[:16]
    
    def get(self, event) -> Optional[Any]:
        """Get cached analysis for event.
        
        Args:
            event: Event to look up.
            
        Returns:
            Cached Analysis object or None if not found/expired.
        """
        # 1. Try exact match
        signature = self._compute_signature(event)
        
        if signature in self.cache:
            return self._handle_hit(signature)
            
        # 2. Try fuzzy match
        fuzzy_signature = self._compute_fuzzy_signature(event)
        if fuzzy_signature in self.cache:
            return self._handle_hit(fuzzy_signature)
            
        self.misses += 1
        return None

    def _handle_hit(self, signature: str) -> Optional[Any]:
        """Process a cache hit."""
        cached = self.cache[signature]
        
        # Check if expired
        if time.time() - cached.timestamp > self.ttl_seconds:
            del self.cache[signature]
            self.misses += 1
            return None
        
        # Cache hit - move to end (LRU)
        self.cache.move_to_end(signature)
        cached.hit_count += 1
        self.hits += 1
        
        return cached.analysis
    
    def put(self, event, analysis: Any) -> None:
        """Cache an analysis result.
        
        Args:
            event: Event that was analyzed.
            analysis: Analysis result to cache.
        """
        # Add both exact and fuzzy signatures to cache
        # This allows future lookups to hit via either
        exact_sig = self._compute_signature(event)
        fuzzy_sig = self._compute_fuzzy_signature(event)
        
        cached_obj = CachedAnalysis(
            analysis=analysis,
            timestamp=time.time(),
            hit_count=0
        )
        
        # Store under both (OrderedDict handles updates)
        self.cache[exact_sig] = cached_obj
        self.cache[fuzzy_sig] = cached_obj
        
        # Enforce max size (LRU eviction)
        if len(self.cache) > self.max_entries:
            # Remove oldest (first item in OrderedDict)
            self.cache.popitem(last=False)
    
    def clear(self) -> None:
        """Clear the cache."""
        self.cache.clear()
        self.hits = 0
        self.misses = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics.
        
        Returns:
            Dictionary with cache stats.
        """
        total_requests = self.hits + self.misses
        hit_rate = (self.hits / total_requests * 100) if total_requests > 0 else 0
        
        return {
            "total_requests": total_requests,
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "hit_rate_percent": round(hit_rate, 1),
            "cached_entries": len(self.cache),
            "max_entries": self.max_entries
        }
    
    def cleanup_expired(self) -> int:
        """Remove expired entries from cache.
        
        Returns:
            Number of entries removed.
        """
        current_time = time.time()
        expired_keys = [
            key for key, cached in self.cache.items()
            if current_time - cached.timestamp > self.ttl_seconds
        ]
        
        for key in expired_keys:
            del self.cache[key]
        
        return len(expired_keys)
