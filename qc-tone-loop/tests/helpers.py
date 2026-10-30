"""Helpers shared by the qcsuite tests (import with ``from helpers import ...``)."""


def wait_job(client, job_id, timeout=60.0):
    """Poll a web job until it finishes; return its detail."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/jobs/{job_id}").json()
        if detail["summary"]["status"] not in ("new", "running"):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def route_usb(device, row=0):
    """Point a row at USB 5 so the fake reamp hears the DI."""
    device.apply_patch({"ops": [{"op": "set_input", "row": row, "port": "USB_5"}]})
