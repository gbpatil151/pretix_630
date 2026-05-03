class PluginRegistry:
    def __init__(self):
        self._cache = None

    def get_or_create(self, **kwargs):
        if self._cache is not None:
            return self._cache
        self._cache = self._collect(**kwargs)
        return self._cache

    def _collect(self, **kwargs):
        raise NotImplementedError

    def reset(self):
        self._cache = None
