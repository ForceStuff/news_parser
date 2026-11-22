from app.adapters.fsp import FspAdapter

ADAPTERS = {"fsp": FspAdapter}


def get_adapter(name: str):
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown adapter: {name}") from exc
