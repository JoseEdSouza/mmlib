from dataclasses import dataclass


@dataclass
class MMLIBConfig:
    use_cache: bool = True
    cache_size: int = 5


config = MMLIBConfig()
