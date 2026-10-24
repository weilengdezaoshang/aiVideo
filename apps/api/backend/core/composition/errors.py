"""Stable layout failures. Callers map these onto existing task failure codes."""


class CompositionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
