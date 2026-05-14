"""Three-step VASP workflow orchestration with resume support."""

import os
import shutil
import logging
from typing import Optional

from .config import write_incar, write_kpoints
from .slurm import submit_job
from .monitor import wait_for_job, JobState, check_outcar_done, check_outcar_errors
from .postprocess import generate_kpoints_vaspkit
from .postprocess import generate_potcar, generate_kpoints_vaspkit

logger = logging.getLogger(__name__)

DONE_MARKER = "{step}_DONE"
FAILED_MARKER = "{step}_FAILED"


def _ensure_input_files(src_dir: str, dst_dir: str, files: list[str]):
    """Copy essential VASP input files to job directory if missing."""
    os.makedirs(dst_dir, exist_ok=True)
    for fname in files:
        src = os.path.join(src_dir, fname)
        dst = os.path.join(dst_dir, fname)
        if os.path.isfile(dst):
            continue
        if os.path.isfile(src):
            shutil.copy2(src, dst)
        else:
            logger.warning(f"Input file '{fname}' not found in {src_dir}")


def _step_is_done(job_dir: str, step_name: str) -> bool:
    """Check if a step has already completed successfully.

    Checks: (1) _DONE marker file, or (2) OUTCAR with finish line.
    """
    done_file = os.path.join(job_dir, DONE_MARKER.format(step=step_name))
    if os.path.isfile(done_file):
        return True
    outcar = os.path.join(job_dir, "OUTCAR")
    if check_outcar_done(outcar):
        # Write marker so next run detects it faster
        with open(done_file, "w") as f:
            f.write("detected from OUTCAR\n")
        return True
    return False


def _step_is_failed(job_dir: str, step_name: str) -> bool:
    """Check if a step has failed (marker present or OUTCAR with errors).

    Only checks OUTCAR if it exists — an empty directory is not 'failed',
    it just hasn't been run yet.
    """
    failed_file = os.path.join(job_dir, FAILED_MARKER.format(step=step_name))
    if os.path.isfile(failed_file):
        return True
    outcar = os.path.join(job_dir, "OUTCAR")
    if not os.path.isfile(outcar):
        return False
    errors = check_outcar_errors(outcar)
    return len(errors) > 0


def _generate_inputs(cfg: dict, work_dir: str):
    """Generate POTCAR and KPOINTS if configured and missing.

    Args:
        cfg: Full configuration dict.
        work_dir: Base working directory.
    """
    # --- POTCAR ---
    potcar_cfg = cfg.get("potcar", {})
    potcar_path = os.path.join(work_dir, "POTCAR")
    if potcar_cfg.get("auto_generate", True) and not os.path.isfile(potcar_path):
        logger.info("POTCAR not found, generating via vaspkit ...")
        if not generate_potcar(work_dir):
            raise RuntimeError("POTCAR generation failed. Check vaspkit and VASP_POT_PATH.")

    # --- KPOINTS ---
    kpts_cfg = cfg.get("kpoints", {})
    kpoints_path = os.path.join(work_dir, "KPOINTS")
    if not kpts_cfg.get("auto_generate", True) or os.path.isfile(kpoints_path):
        if os.path.isfile(kpoints_path):
            logger.info("KPOINTS exists, using existing file")
        return

    kpts_mode = kpts_cfg.get("mode", "direct")

    if kpts_mode == "vaspkit":
        scheme = kpts_cfg.get("scheme", "M")
        density = kpts_cfg.get("kpoints_density", 0.04)
        logger.info("KPOINTS not found, generating via vaspkit ...")
        if not generate_kpoints_vaspkit(work_dir, scheme=scheme, density=density):
            raise RuntimeError("KPOINTS generation via vaspkit failed.")
    else:
        # mode == "direct": write KPOINTS using config params or auto-calculate
        scheme = kpts_cfg.get("scheme", "M")
        mesh = kpts_cfg.get("mesh", [0, 0, 0])
        density = kpts_cfg.get("kpoints_density", 0.04)
        poscar = os.path.join(work_dir, "POSCAR")
        logger.info(f"KPOINTS not found, writing directly (scheme={scheme}, density={density}) ...")
        write_kpoints(kpoints_path, scheme=scheme, mesh=mesh,
                      kpoints_density=density, poscar_path=poscar)


def _count_kpoints(kpts_path: str) -> int | None:
    """Parse KPOINTS file and return total number of K-points in mesh.

    Handles both mesh format (Nx Ny Nz) and K-spacing format.
    Returns None if parsing fails.
    """
    try:
        with open(kpts_path) as f:
            lines = f.readlines()
        if len(lines) < 5:
            return None
        # Line 4 (0-indexed: 3) is the mesh line
        parts = lines[3].strip().split()
        if len(parts) >= 3:
            kx, ky, kz = int(parts[0]), int(parts[1]), int(parts[2])
            total = kx * ky * kz
            return total
        # K-spacing format: single number, total K-points unknown
        return None
    except (OSError, ValueError, IndexError):
        return None


def _prepare_step_directory(step_name: str, step_cfg: dict, work_dir: str,
                            steps_cfg: dict) -> str:
    """Prepare input files for a step directory.

    Returns the job directory path.
    """
    job_dir = os.path.join(work_dir, step_cfg.get("dir", step_name))
    os.makedirs(job_dir, exist_ok=True)

    # Copy essential input files from work_dir if missing in job_dir
    _ensure_input_files(work_dir, job_dir, ["POTCAR", "KPOINTS", "POSCAR"])

    # Pre-step file copies
    if step_name == "scf":
        # Try opt dir first, then work_dir (when opt skipped via --from)
        opt_dir = os.path.join(work_dir, steps_cfg["opt"].get("dir", "opt"))
        contcar = os.path.join(opt_dir, "CONTCAR")
        if not os.path.isfile(contcar):
            contcar = os.path.join(work_dir, "CONTCAR")
        if os.path.isfile(contcar):
            dst = os.path.join(job_dir, "POSCAR")
            if not os.path.isfile(dst) or os.path.getmtime(contcar) > os.path.getmtime(dst):
                shutil.copy2(contcar, dst)
                logger.info(f"Copied CONTCAR -> {dst}")

    elif step_name == "dos":
        # Generate denser KPOINTS via vaspkit 102 (Gamma, 0.02 spacing)
        dos_kpts = step_cfg.get("kpoints", {})
        dos_density = dos_kpts.get("kpoints_density", 0.02)
        logger.info(f"Generating DOS KPOINTS via vaspkit (spacing={dos_density}, Gamma)...")
        if not generate_kpoints_vaspkit(job_dir, scheme="G", density=dos_density):
            logger.warning("vaspkit KPOINTS generation failed, using fallback")
            scheme_str = "Gamma"
            kpts_path = os.path.join(job_dir, "KPOINTS")
            with open(kpts_path, "w") as f:
                f.write(f"K-Spacing Value: {dos_density:.3f}\n")
                f.write("0\n")
                f.write(f"{scheme_str}\n")
                f.write(f"{dos_density:.6f}\n")
            logger.info(f"Written fallback KPOINTS for DOS (K-spacing={dos_density}, {scheme_str})")

        # Auto-adjust ISMEAR if total K-points <= 4 (tetrahedron method needs > 4)
        kpts_path = os.path.join(job_dir, "KPOINTS")
        total_kpts = _count_kpoints(kpts_path)
        if total_kpts is not None and total_kpts <= 4:
            logger.warning(f"Total K-points = {total_kpts} <= 4, forcing ISMEAR=0 instead of -5")
            step_cfg.setdefault("incar", {})["ISMEAR"] = 0
        else:
            dos_ismear = step_cfg.get("incar", {}).get("ISMEAR", -5)
            logger.info(f"Total K-points = {total_kpts}, keeping ISMEAR={dos_ismear}")

        # Copy CHGCAR: try scf dir first, then work_dir
        scf_dir = os.path.join(work_dir, steps_cfg["scf"].get("dir", "scf"))
        chgcar = os.path.join(scf_dir, "CHGCAR")
        if not os.path.isfile(chgcar):
            chgcar = os.path.join(work_dir, "CHGCAR")
        if os.path.isfile(chgcar):
            dst = os.path.join(job_dir, "CHGCAR")
            if not os.path.isfile(dst) or os.path.getmtime(chgcar) > os.path.getmtime(dst):
                shutil.copy2(chgcar, dst)
                logger.info(f"Copied CHGCAR -> {dst}")
        else:
            logger.warning(f"CHGCAR not found in {scf_dir} or {work_dir}, DOS step may fail")

    # Write INCAR
    incar_path = os.path.join(job_dir, "INCAR")
    write_incar(incar_path, step_cfg.get("incar", {}))
    logger.info(f"Written INCAR for '{step_name}' -> {incar_path}")

    return job_dir


def run_step(
    step_name: str,
    step_cfg: dict,
    work_dir: str,
    slurm_cfg: dict,
    vasp_exec: str,
    poll_interval: int = 60,
    dependency_jobid: Optional[str] = None,
    dry_run: bool = False,
    retry_count: int = 0,
    max_retries: int = 1,
    pre_exec_cmds: list[str] | None = None,
) -> Optional[str]:
    """Run a single VASP calculation step.

    Returns:
        Slurm JOBID string, or None if dry_run.
    """
    if not step_cfg.get("enabled", True):
        logger.info(f"Step '{step_name}' is disabled, skipping")
        return None

    job_dir = os.path.join(work_dir, step_cfg.get("dir", step_name))
    os.makedirs(job_dir, exist_ok=True)

    # Copy essential input files
    _ensure_input_files(work_dir, job_dir, ["POTCAR", "KPOINTS", "POSCAR"])

    # Write INCAR
    incar_path = os.path.join(job_dir, "INCAR")
    write_incar(incar_path, step_cfg.get("incar", {}))
    logger.info(f"Written INCAR for '{step_name}' -> {incar_path}")

    # Submit
    job_name = step_cfg.get("job_name", step_name)
    full_job_name = f"{slurm_cfg.get('job_name_prefix', 'vasp')}_{job_name}"
    if retry_count > 0:
        full_job_name += f"_r{retry_count}"

    jobid = submit_job(
        job_name=full_job_name,
        job_dir=job_dir,
        slurm_cfg=slurm_cfg,
        vasp_exec=vasp_exec,
        dependency_jobid=dependency_jobid,
        dry_run=dry_run,
        pre_exec_cmds=pre_exec_cmds,
    )

    if dry_run:
        logger.info(f"[DRY RUN] Would submit {full_job_name} in {job_dir}")
        return None

    logger.info(f"Submitted {full_job_name} -> JobID={jobid}")
    return jobid


def _wait_and_handle_failure(step_name: str, jobid: str, job_dir: str,
                             poll_interval: int, max_retries: int,
                             max_wait: int | None = None) -> JobState:
    """Wait for a job and return its final state. No retry logic here —
    retry is handled at the workflow level."""
    outcar = os.path.join(job_dir, "OUTCAR")
    logger.info(f"Waiting for {step_name} (JobID={jobid}) to complete ...")
    state = wait_for_job(
        jobid=jobid,
        outcar_path=outcar,
        poll_interval=poll_interval,
        timeout=max_wait,
    )

    if state == JobState.COMPLETED:
        logger.info(f"Step '{step_name}' completed successfully")
    else:
        logger.error(f"Step '{step_name}' finished with state {state.value}")
        errors = check_outcar_errors(outcar)
        if errors:
            logger.error(f"OUTCAR errors: {errors}")

    return state


def run_workflow(cfg: dict, step_filter: Optional[str] = None, dry_run: bool = False):
    """Execute the full VASP workflow.

    Args:
        cfg: Full configuration dict from config.load_config().
        step_filter: If set, only run this single step.
        dry_run: If True, generate scripts but don't submit.
    """
    work_dir = cfg["work_dir"]
    slurm_cfg = cfg["slurm"]
    vasp_exec = cfg["vasp"]["exec"]
    poll_interval = cfg["poll_interval"]
    mode = cfg["mode"]
    steps_cfg = cfg["steps"]
    resume_cfg = cfg.get("resume", {})
    skip_completed = resume_cfg.get("skip_completed", True)
    retry_failed = resume_cfg.get("retry_failed", True)
    max_retries = resume_cfg.get("max_retries", 1)

    # ---- Step 0: Generate input files ----
    _generate_inputs(cfg, work_dir)

    # Validate minimum required inputs exist
    for fname in ["POSCAR", "POTCAR", "KPOINTS"]:
        path = os.path.join(work_dir, fname)
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Required input file '{fname}' not found in {work_dir}. "
                f"Ensure POSCAR exists and POTCAR/KPOINTS auto-generation is enabled."
            )

    step_order = ["opt", "scf", "dos"]

    if step_filter:
        if step_filter not in step_order:
            raise ValueError(f"Unknown step '{step_filter}', must be one of {step_order}")
        step_order = [step_filter]

    jobids = {}

    for step_name in step_order:
        step_cfg = steps_cfg.get(step_name, {})

        if not step_cfg.get("enabled", True):
            logger.info(f"Skipping disabled step: {step_name}")
            continue

        job_dir = os.path.join(work_dir, step_cfg.get("dir", step_name))

        # ---- Check if already done ----
        if skip_completed and _step_is_done(job_dir, step_name):
            logger.info(f"Step '{step_name}' already completed — skipping")
            continue

        # ---- Check if previously failed ----
        if _step_is_failed(job_dir, step_name):
            if retry_failed:
                logger.warning(f"Step '{step_name}' previously failed — retrying ...")
                # Clean up old failed marker
                failed_file = os.path.join(job_dir, FAILED_MARKER.format(step=step_name))
                if os.path.isfile(failed_file):
                    os.remove(failed_file)
            else:
                logger.warning(f"Step '{step_name}' previously failed and retry is disabled — skipping")
                continue

        # ---- Prepare directory and input files ----
        _prepare_step_directory(step_name, step_cfg, work_dir, steps_cfg)

        # ---- Determine dependency ----
        dependency_jobid = None
        if mode == "chain":
            if step_name == "scf" and "opt" in jobids:
                dependency_jobid = jobids["opt"]
            elif step_name == "dos" and "scf" in jobids:
                dependency_jobid = jobids["scf"]

        # ---- Pre-exec commands for chain mode DOS step ----
        pre_exec_cmds = None
        if step_name == "dos":
            pre_exec_cmds = [
                'echo "Waiting 10s for filesystem sync before DOS..."',
                'sleep 10',
            ]

        # ---- Submit and optionally wait ----
        for attempt in range(max_retries + 1):
            jobid = run_step(
                step_name=step_name,
                step_cfg=step_cfg,
                work_dir=work_dir,
                slurm_cfg=slurm_cfg,
                vasp_exec=vasp_exec,
                poll_interval=poll_interval,
                dependency_jobid=dependency_jobid,
                dry_run=dry_run,
                retry_count=attempt,
                max_retries=max_retries,
                pre_exec_cmds=pre_exec_cmds,
            )

            if dry_run or jobid is None:
                break

            jobids[step_name] = jobid

            if mode == "sequential":
                max_wait = cfg.get("max_wait", None)
                state = _wait_and_handle_failure(
                    step_name, jobid, job_dir, poll_interval, max_retries,
                    max_wait=max_wait,
                )

                if state == JobState.COMPLETED:
                    break  # success, move to next step
                elif attempt < max_retries:
                    logger.warning(f"Retrying step '{step_name}' (attempt {attempt + 2}/{max_retries + 1}) ...")
                else:
                    raise RuntimeError(
                        f"Step '{step_name}' (JobID={jobid}) failed after "
                        f"{max_retries + 1} attempt(s). Aborting."
                    )
            else:
                break  # chain mode: no wait, move on

    # ---- Summary ----
    if mode == "chain" and jobids:
        last_step = step_order[-1]
        if last_step in jobids:
            logger.info(
                f"Chain mode: all jobs submitted. Last = {last_step} "
                f"(JobID={jobids[last_step]})"
            )

    logger.info("=" * 50)
    logger.info("Workflow summary:")
    for sn, jid in jobids.items():
        status = "submitted"
        jd = os.path.join(work_dir, steps_cfg[sn].get("dir", sn))
        if _step_is_done(jd, sn):
            status = "completed"
        elif _step_is_failed(jd, sn):
            status = "failed"
        logger.info(f"  {sn}: JobID={jid} ({status})")

    if mode == "sequential" and not dry_run:
        all_done = all(
            _step_is_done(os.path.join(work_dir, steps_cfg[s].get("dir", s)), s)
            for s, j in jobids.items() if j
        )
        if all_done:
            logger.info("All steps completed successfully!")

    return jobids
