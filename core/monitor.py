"""Slurm job state monitoring and OUTCAR error detection."""

import os
import re
import subprocess
import time
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class JobState(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


def _get_job_state_slurm(jobid: str) -> tuple[JobState, str]:
    """Query Slurm for job state via sacct.

    Returns (JobState, message).
    """
    cmd = [
        "sacct", "-j", jobid,
        "--format=State,ExitCode",
        "--noheader",
        "--parsable2",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return JobState.UNKNOWN, "sacct timed out"
    except FileNotFoundError:
        return JobState.UNKNOWN, "sacct not found — not on a Slurm system?"

    if result.returncode != 0:
        return JobState.UNKNOWN, f"sacct error: {result.stderr.strip()}"

    lines = result.stdout.strip().split("\n")
    if not lines:
        return JobState.UNKNOWN, "sacct returned no output"

    # Parse the most relevant line (first job step, the .batch line)
    # States: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, TIMEOUT, etc.
    for line in lines:
        fields = line.split("|")
        if len(fields) < 2:
            continue
        state_raw = fields[0].strip()
        exit_code = fields[1].strip() if len(fields) > 1 else ""

        # Handle job array or step entries
        for state_str in state_raw.split(","):
            state_str = state_str.strip()

            # Handle composite states like "COMPLETED+0:0"
            # Split on the first non-alpha char to get base state
            base_state = _parse_base_state(state_str)

            if base_state in ("COMPLETED",):
                if exit_code and exit_code not in ("0:0",):
                    # Non-zero exit code means failure
                    pass  # fall through to other lines
                return JobState.COMPLETED, f"Exit: {exit_code}"
            elif base_state in ("FAILED",):
                return JobState.FAILED, f"Exit: {exit_code}"
            elif base_state in ("TIMEOUT", "CANCELLED", "DEADLINE"):
                return JobState.TIMEOUT, f"State: {base_state}"
            elif base_state in ("PENDING",):
                return JobState.PENDING, "Waiting in queue"
            elif base_state in ("RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED"):
                return JobState.RUNNING, f"State: {base_state}"

    return JobState.UNKNOWN, f"Unrecognized sacct output: {lines[0][:80]}"


def _parse_base_state(raw_state: str) -> str:
    """Extract base Slurm state from composite states like 'COMPLETED+0:0'."""
    # Remove everything after the first non-alpha character
    match = re.match(r"^([A-Za-z_]+)", raw_state)
    if match:
        return match.group(1)
    return raw_state


def check_outcar_errors(outcar_path: str) -> list[str]:
    """Parse OUTCAR for error signatures.

    Returns list of error descriptions found (empty = looks OK).
    """
    errors = []

    if not os.path.isfile(outcar_path):
        return ["OUTCAR not found"]

    with open(outcar_path, "r", errors="replace") as f:
        # Read last 500 lines for performance
        content = f.read()
        tail = content[-200000:] if len(content) > 200000 else content

    # Check for catastrophic VASP errors
    if "SIGSEGV" in tail or "segmentation fault" in tail.lower():
        errors.append("Segmentation fault detected")
    if "ZPOTRF" in tail:
        errors.append("ZPOTRF — possible electronic instability")
    if "EDDDAV" in tail:
        errors.append("EDDDAV — error in electronic minimization")

    # Check if VASP stopped due to reaching electronic step limit
    if "WARNING: electronic step limit" in tail:
        errors.append("Electronic step limit reached — SCF not converged")

    # Check for "EEEE" pattern (numerical overflow in VASP output)
    if re.search(r"E{5,}", tail):
        errors.append("Possible numerical overflow (EEEEE pattern)")

    # Check for subroutine error call traces
    if "ERROR" in tail and "Call stack" in tail:
        errors.append("VASP internal ERROR with call stack")

    return errors


def check_outcar_done(outcar_path: str) -> bool:
    """Check if OUTCAR signals successful completion."""
    if not os.path.isfile(outcar_path):
        return False

    try:
        with open(outcar_path, "r", errors="replace") as f:
            # Check last 5KB for the finish line
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 5000)
            f.seek(max(0, size - read_size))
            tail = f.read()
    except OSError:
        return False

    # VASP writes this line at successful completion
    if "General timing and accounting" in tail:
        return True

    return False


def wait_for_job(
    jobid: str,
    outcar_path: str,
    poll_interval: int = 60,
    timeout: Optional[int] = None,
    callback=None,
) -> JobState:
    """Poll until job completes, fails, or times out.

    Args:
        jobid: Slurm job ID.
        outcar_path: Path to OUTCAR file to check.
        poll_interval: Seconds between polls.
        timeout: Max wait seconds (None = no timeout).
        callback: Called on each poll with JobState.

    Returns:
        Final JobState.
    """
    start_time = time.time()
    last_errors = []

    while True:
        if timeout and (time.time() - start_time) > timeout:
            logger.error(f"Job {jobid} timed out after {timeout}s")
            return JobState.TIMEOUT

        state, msg = _get_job_state_slurm(jobid)

        # Check OUTCAR for early error detection while running
        if state == JobState.RUNNING:
            errors = check_outcar_errors(outcar_path)
            if errors and errors != last_errors:
                logger.warning(f"OUTCAR errors detected: {errors}")

        if state == JobState.COMPLETED:
            # Double-check OUTCAR for completion
            if check_outcar_done(outcar_path):
                logger.info(f"Job {jobid} completed (OUTCAR verified)")
                # Also check for late errors
                errors = check_outcar_errors(outcar_path)
                if errors:
                    logger.warning(f"Job completed but with OUTCAR warnings: {errors}")
                return JobState.COMPLETED
            else:
                logger.warning(
                    f"Slurm says COMPLETED but OUTCAR missing finish line — "
                    f"job may have been cancelled"
                )
                return JobState.FAILED

        if state == JobState.FAILED:
            errors = check_outcar_errors(outcar_path)
            logger.error(f"Job {jobid} failed. OUTCAR errors: {errors}")
            return JobState.FAILED

        if state == JobState.TIMEOUT:
            logger.error(f"Job {jobid} hit Slurm TIME limit")
            return JobState.TIMEOUT

        if state == JobState.RUNNING:
            elapsed = int(time.time() - start_time)
            logger.info(f"Job {jobid} still running ({elapsed}s elapsed) ...")

        if state == JobState.PENDING:
            elapsed = int(time.time() - start_time)
            logger.info(f"Job {jobid} still pending ({elapsed}s) ...")

        if callback:
            callback(state)

        time.sleep(poll_interval)


def check_job_status(jobid: str) -> dict:
    """One-shot check of job status. Returns dict with state info."""
    state, msg = _get_job_state_slurm(jobid)
    return {
        "jobid": jobid,
        "state": state.value,
        "message": msg,
    }
