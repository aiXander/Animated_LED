from .base import Effect

_REGISTRY: dict[str, type[Effect]] = {}


def register(cls: type[Effect]) -> type[Effect]:
    """Register an Effect subclass under its `name` so the API/MCP can list it."""
    if cls.name == "base":
        raise ValueError("refusing to register an Effect with the default name 'base'")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"effect name {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def get_effect_class(name: str) -> type[Effect]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown effect: {name!r}")
    return _REGISTRY[name]


def list_effects() -> dict[str, type[Effect]]:
    return dict(_REGISTRY)
