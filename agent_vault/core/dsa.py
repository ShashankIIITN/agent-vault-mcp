import hashlib
import os
import heapq

class BloomFilter:
    def __init__(self, size=1000000, hash_count=5):
        self.size = size
        self.hash_count = hash_count
        self.bit_array = [False] * size

    def _hashes(self, item):
        # Simple string-based hashing for simplicity and to avoid external dependencies
        hashes = []
        for i in range(self.hash_count):
            # Using md5 for fast hashing across multiple seeds
            h = int(hashlib.md5(f"{item}:{i}".encode('utf-8')).hexdigest(), 16)
            hashes.append(h % self.size)
        return hashes

    def add(self, item):
        for h in self._hashes(item):
            self.bit_array[h] = True

    def check(self, item):
        for h in self._hashes(item):
            if not self.bit_array[h]:
                return False
        return True

class TokenBoundedMinHeap:
    def __init__(self, max_tokens):
        self.max_tokens = max_tokens
        self.current_tokens = 0
        self.heap = [] # stores tuples of (score, tiebreaker, tokens, item)
        self.tiebreaker = 0

    def add(self, item, score, tokens):
        if tokens > self.max_tokens:
            return

        # We want to keep items with HIGHEST score. 
        # Min-heap pops the SMALLEST score first.
        # So we push (score, ...), and pop when current_tokens > max_tokens.
        # This will remove the lowest-scored items.
        heapq.heappush(self.heap, (score, self.tiebreaker, tokens, item))
        self.tiebreaker += 1
        self.current_tokens += tokens

        while self.current_tokens > self.max_tokens and self.heap:
            popped = heapq.heappop(self.heap)
            self.current_tokens -= popped[2]
            
    def get_items(self):
        # Return items sorted by score descending (highest score first)
        sorted_items = sorted(self.heap, key=lambda x: x[0], reverse=True)
        return [x[3] for x in sorted_items]

def get_file_digest(filepath):
    """Returns SHA-256 digest of a file, and its size, or (None, 0) if it doesn't exist."""
    if not os.path.exists(filepath):
        return None, 0
    hasher = hashlib.sha256()
    size_in_bytes = 0
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
                size_in_bytes += len(chunk)
        return hasher.hexdigest(), size_in_bytes
    except Exception:
        return None, 0
