from pydantic import BaseModel


class WatchPoint(BaseModel):
    var: str
    log_location: str

def get_runtime_feedback(cmd: list[str], stdin: bytes | None, watchpoints_list: list[dict], monitor_locations: list[str]) -> dict:
    watchpoints = [
        WatchPoint(**wp)
        for wp in watchpoints_list
    ]
    pass
