from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    RESPONDING = "RESPONDING"
    TOOL_RUNNING = "TOOL_RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


ALLOWED: dict[State, set[State]] = {
    State.IDLE: {State.LISTENING, State.COMPLETED, State.ERROR},
    # LISTENING -> RESPONDING/TOOL_RUNNING: el VAD del proveedor puede iniciar la respuesta
    # antes de que nuestro VAD marque fin de habla.
    State.LISTENING: {State.PROCESSING, State.RESPONDING, State.TOOL_RUNNING, State.LISTENING, State.COMPLETED, State.ERROR},
    State.PROCESSING: {State.RESPONDING, State.TOOL_RUNNING, State.LISTENING, State.COMPLETED, State.ERROR},
    State.RESPONDING: {State.LISTENING, State.INTERRUPTED, State.TOOL_RUNNING, State.COMPLETED, State.ERROR},
    State.TOOL_RUNNING: {State.PROCESSING, State.RESPONDING, State.INTERRUPTED, State.COMPLETED, State.ERROR},
    State.INTERRUPTED: {State.LISTENING, State.PROCESSING, State.COMPLETED, State.ERROR},
    State.COMPLETED: set(),
    State.ERROR: {State.IDLE, State.COMPLETED},
}


class InvalidTransition(Exception):
    pass


class SessionStateMachine:
    def __init__(self) -> None:
        self.state = State.IDLE

    def to(self, new: State) -> State:
        if new == self.state:
            return self.state
        if new not in ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state} -> {new}")
        self.state = new
        return new
