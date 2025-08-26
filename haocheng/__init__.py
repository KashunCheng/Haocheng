from pydantic import BaseModel


class WatchPoint(BaseModel):
    var: str
    log_location: str


class RuntimeFeedback(BaseModel):
    # dict["file:line", list(occurrence)[dict["var", "value"]]]
    watchpoints: dict[str, list[dict[str, str]]]
    # dict["file:line", list(occurrence)["backtrace string"]]
    breakpoints: dict[str, list[str]]


def get_runtime_feedback(cmd: list[str], stdin: bytes | None, watchpoints_list: list[dict],
                         monitor_locations: list[str]) -> RuntimeFeedback:
    watchpoints = [
        WatchPoint(**wp)
        for wp in watchpoints_list
    ]
    return RuntimeFeedback(
        watchpoints={},
        breakpoints={},
    )
