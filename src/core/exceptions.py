class JobNotFound(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class TerminalConflict(Exception):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"Cannot cancel a {status} job")


class InvalidCursor(Exception):
    pass
