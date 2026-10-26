import importlib

# adapter name in models.yaml -> "module:Class" (imported lazily so torch loads only when needed)
ADAPTERS = {
    "moss_local": "local_tts.adapters.moss_local:MossLocalAdapter",
}


def adapter_class(name: str):
    module, cls = ADAPTERS[name].split(":")
    return getattr(importlib.import_module(module), cls)
