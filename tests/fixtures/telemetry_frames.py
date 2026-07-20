RAW_TORCS_FRAME = {
    "seq": "12", "sim_time": "3.5", "lap": "2", "speedX": "201.25",
    "speedY": "1.5", "rpm": "8100", "gear": "5", "fuel": "30.2",
    "damage": "10", "trackPos": "0.2", "curLapTime": "44.1",
    "lastLapTime": "90.3", "racePos": "3", "throttle": "0.8",
    "brake": "0.0", "steer": "-0.1", "distFromStart": "1234.5",
    "distRaced": "4567.8",
    **{f"track_{index}": str(index) for index in range(19)},
    **{f"opponent_{index}": str(index + 20) for index in range(36)},
}
