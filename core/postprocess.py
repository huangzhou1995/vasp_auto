"""Input file generation and post-processing with vaspkit."""

import os
import subprocess
import logging

logger = logging.getLogger(__name__)


def _run_vaspkit(input_text: str, work_dir: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run vaspkit with given input text, return CompletedProcess."""
    try:
        result = subprocess.run(
            ["vaspkit"],
            input=input_text,
            capture_output=True,
            text=True,
            cwd=work_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("vaspkit timed out")
        raise
    except FileNotFoundError:
        logger.error("vaspkit not found in PATH")
        raise

    if result.returncode != 0:
        logger.warning(f"vaspkit exit code {result.returncode}")

    # Show tail of output
    output_lines = result.stdout.strip().split("\n")
    tail = output_lines[-8:] if len(output_lines) > 8 else output_lines
    logger.debug("vaspkit output:\n" + "\n".join(tail))

    return result


def generate_potcar(work_dir: str):
    """Generate POTCAR from POSCAR using vaspkit task 103.

    vaspkit 103 reads POSCAR, maps elements to PBE POTCARs, builds POTCAR.
    Requires VASP_POT_PATH or ~/.vaspkit to be configured.

    Args:
        work_dir: Directory containing POSCAR.
    """
    poscar = os.path.join(work_dir, "POSCAR")
    if not os.path.isfile(poscar):
        logger.warning(f"POSCAR not found in {work_dir}, cannot generate POTCAR")
        return False

    potcar = os.path.join(work_dir, "POTCAR")
    if os.path.isfile(potcar):
        logger.info("Removing old POTCAR before regeneration ...")
        os.remove(potcar)

    logger.info("Generating POTCAR via vaspkit task 103 ...")
    # Task 103: generate POTCAR, then quit
    _run_vaspkit("103\nq\n", work_dir, timeout=120)

    if os.path.isfile(potcar):
        logger.info("POTCAR generated successfully")
        return True
    else:
        logger.error("POTCAR generation failed — is VASP_POT_PATH set in ~/.vaspkit?")
        return False


def generate_kpoints_vaspkit(work_dir: str, scheme: str = "M", density: float = 0.04):
    """Generate KPOINTS using vaspkit task 102.

    Args:
        work_dir: Directory containing POSCAR.
        scheme: 'G' (Gamma-centered) or 'M' (Monkhorst-Pack).
        density: K-point density in points per inverse Angstrom.
    """
    poscar = os.path.join(work_dir, "POSCAR")
    if not os.path.isfile(poscar):
        logger.warning(f"POSCAR not found in {work_dir}, cannot generate KPOINTS")
        return False

    logger.info(f"Generating KPOINTS via vaspkit task 102 (scheme={scheme}, density={density}) ...")

    # vaspkit 102 interaction:
    # 1. Enter task number: 102
    # 2. Select scheme: 1=Gamma, 2=MP
    # 3. Enter k-point density
    # 4. Press Enter to confirm
    # 5. q to quit
    scheme_num = "1" if scheme.upper() == "G" else "2"
    inputs = f"102\n{scheme_num}\n{density}\n\nq\n"

    _run_vaspkit(inputs, work_dir, timeout=60)

    kpoints = os.path.join(work_dir, "KPOINTS")
    if os.path.isfile(kpoints):
        logger.info("KPOINTS generated successfully via vaspkit")
        return True
    else:
        logger.error("KPOINTS generation via vaspkit failed")
        return False


def run_vaspkit_tasks(task_numbers: list[int], work_dir: str):
    """Run a list of vaspkit post-processing tasks in sequence.

    Args:
        task_numbers: List of vaspkit task numbers (e.g. [301, 211]).
        work_dir: Directory containing VASP output files.
    """
    if not task_numbers:
        return

    inputs = []
    for t in task_numbers:
        inputs.append(str(t))
        inputs.extend([""] * 20)

    inputs.append("q")
    input_str = "\n".join(inputs)

    logger.info(f"Running vaspkit tasks {task_numbers} in {work_dir}")
    result = _run_vaspkit(input_str, work_dir, timeout=120)

    output_lines = result.stdout.strip().split("\n")
    tail = output_lines[-10:] if len(output_lines) > 10 else output_lines
    if tail:
        logger.info("vaspkit output (last lines):\n" + "\n".join(tail))


def run_postprocessing(cfg: dict):
    """Run all configured post-processing tasks.

    Args:
        cfg: Full configuration dict.
    """
    pp_cfg = cfg.get("postprocess", {})
    task_numbers = pp_cfg.get("vaspkit_tasks", [])
    work_dir = cfg["work_dir"]

    # For DOS processing, use the dos step directory
    dos_dir = os.path.join(work_dir, cfg["steps"]["dos"].get("dir", "dos"))
    if os.path.isdir(dos_dir):
        work_dir = dos_dir

    run_vaspkit_tasks(task_numbers, work_dir)
