"""Slurm job script generation (from config or template) and submission."""

import os
import re
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SLURM_BIN = '/public/softwares/slurm-24/bin'

# Patterns to detect the VASP execution line in user templates
_EXEC_PATTERNS = [
    re.compile(r"(mpirun|srun|exec)\s+.*(vasp_std|vasp_gam|vasp_ncl|qvasp)", re.IGNORECASE),
    re.compile(r"^\s*\$MPIRUN_CMD\s+.*(vasp_std|vasp_gam|vasp_ncl|qvasp|\$\{?EXECUTABLE\}?)", re.IGNORECASE),
    re.compile(r"^\s*\$startexe\b", re.IGNORECASE),
    re.compile(r"^\s*exec\s+\$startexe\b", re.IGNORECASE),
]


def _build_sbatch_script(
    job_name: str,
    job_dir: str,
    slurm_cfg: dict,
    vasp_exec: str,
    pre_exec_cmds: list[str] | None = None,
) -> str:
    """Generate an sbatch script string.

    If slurm_cfg.template is set, uses the user's existing sbatch script
    as a template — preserving module loads, GPU config, MPI setup, etc.
    Otherwise generates a minimal script from slurm_cfg parameters.

    Args:
        job_name: SLURM job name.
        job_dir: Working directory for this job step.
        slurm_cfg: Slurm config dict.
        vasp_exec: VASP executable name.

    Returns:
        Full sbatch script as a string.
    """
    template_path = slurm_cfg.get("template", "").strip()
    if template_path:
        template_path = os.path.expanduser(template_path)
        return _build_from_template(job_name, job_dir, template_path, vasp_exec, pre_exec_cmds)
    return _build_minimal(job_name, job_dir, slurm_cfg, vasp_exec, pre_exec_cmds)


def _build_minimal(job_name: str, job_dir: str, slurm_cfg: dict, vasp_exec: str,
                   pre_exec_cmds: list[str] | None = None) -> str:
    """Generate a minimal sbatch script from config params."""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --partition={slurm_cfg['partition']}",
        f"#SBATCH --nodes={slurm_cfg['nodes']}",
        f"#SBATCH --ntasks-per-node={slurm_cfg['ntasks_per_node']}",
        f"#SBATCH --time={slurm_cfg['time']}",
        f"#SBATCH --output={job_dir}/{job_name}_%j.out",
        f"#SBATCH --error={job_dir}/{job_name}_%j.err",
    ]

    extra = slurm_cfg.get("extra", "").strip()
    if extra:
        for line in extra.split("\n"):
            line = line.strip()
            if line:
                lines.append(line)

    lines += [
        "",
        f"cd {job_dir}",
        "",
        f"echo '=== VASP Job Started at $(date) ==='",
        f"echo 'Job name: {job_name}'",
        "echo 'Hostname: $(hostname)'",
        "",
    ]

    if pre_exec_cmds:
        for cmd in pre_exec_cmds:
            lines.append(cmd)
        lines.append("")

    lines += [
        f"srun {vasp_exec}",
        "",
    ]

    lines += _marker_lines(job_name)
    return "\n".join(lines) + "\n"


def _build_from_template(
    job_name: str,
    job_dir: str,
    template_path: str,
    vasp_exec: str,
    pre_exec_cmds: list[str] | None = None,
) -> str:
    """Build sbatch script from a user-provided template.

    Reads the template, fixes up job name / output / error directives,
    injects cd <job_dir> before execution, and wraps the VASP run command
    with start/end markers and exit code checking.

    Args:
        job_name: SLURM job name for this step.
        job_dir: Working directory for this step.
        template_path: Path to user's sbatch script.
        vasp_exec: VASP executable name (used if template has no exec line).

    Returns:
        Modified sbatch script.
    """
    if not os.path.isfile(template_path):
        raise FileNotFoundError(f"Slurm template not found: {template_path}")

    with open(template_path, "r") as f:
        content = f.read()

    lines = content.split("\n")
    new_lines = []
    exec_line_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()

        # --- Fix up #SBATCH directives ---
        if re.match(r"^#SBATCH\s+--job-name[=\s]", stripped, re.IGNORECASE):
            new_lines.append(f"#SBATCH --job-name={job_name}")
            continue

        if re.match(r"^#SBATCH\s+--output[=\s]", stripped, re.IGNORECASE):
            new_lines.append(f"#SBATCH --output={job_dir}/{job_name}_%j.out")
            continue

        if re.match(r"^#SBATCH\s+--error[=\s]", stripped, re.IGNORECASE):
            new_lines.append(f"#SBATCH --error={job_dir}/{job_name}_%j.err")
            continue

        # --- Detect VASP execution line ---
        if exec_line_idx < 0 and _is_exec_line(stripped):
            exec_line_idx = i
            # Don't append yet — we'll inject cd + wrapper here
            continue

        new_lines.append(line)

    # If no exec line found, add one
    exec_cmd = vasp_exec
    if exec_line_idx >= 0:
        exec_cmd = lines[exec_line_idx].strip()

    # Strip 'exec ' prefix so post-execution marker logic actually runs.
    # 'exec' replaces the shell process — the touch commands would never execute.
    if exec_cmd.startswith("exec "):
        exec_cmd = exec_cmd[5:]
        logger.debug("Stripped 'exec' prefix to allow post-execution marker logic")

    # Build the execution block
    exec_block = [
        "",
        f"cd {job_dir}",
        "",
        "echo '=== VASP Job Started at $(date) ==='",
        f"echo 'Job name: {job_name}'",
        "echo 'Hostname: $(hostname)'",
        "echo 'Working dir: ' $(pwd)",
        "",
    ]

    if pre_exec_cmds:
        for cmd in pre_exec_cmds:
            exec_block.append(cmd)
        exec_block.append("")

    exec_block += [
        exec_cmd,
        "",
    ]
    exec_block += _marker_lines(job_name)

    # Insert the execution block at the position of the original exec line,
    # or at the end if no exec line was found
    if exec_line_idx >= 0:
        # Find where we inserted: count lines before the original exec line
        # in new_lines (accounting for substitutions we already made)
        insert_at = len(new_lines)
        result = "\n".join(new_lines[:insert_at]) + "\n" + "\n".join(exec_block)
        # add remaining lines after the block
        # Actually, we already removed the exec line and added all other lines to new_lines.
        # Let me recalculate: new_lines has everything EXCEPT the exec line and the
        # lines we replaced (job-name, output, error). exec_block goes where the
        # exec line was.
        # Rebuild: everything before exec_line_idx (minus replaced lines)
        # + exec_block + everything after exec_line_idx
        pass

    # Simpler approach: rebuild from scratch
    final_lines = []
    for i, line in enumerate(lines):
        stripped = line.strip()

        if re.match(r"^#SBATCH\s+--job-name[=\s]", stripped, re.IGNORECASE):
            final_lines.append(f"#SBATCH --job-name={job_name}")
        elif re.match(r"^#SBATCH\s+--output[=\s]", stripped, re.IGNORECASE):
            final_lines.append(f"#SBATCH --output={job_dir}/{job_name}_%j.out")
        elif re.match(r"^#SBATCH\s+--error[=\s]", stripped, re.IGNORECASE):
            final_lines.append(f"#SBATCH --error={job_dir}/{job_name}_%j.err")
        elif _is_exec_line(stripped):
            # Insert the execution wrapper here
            final_lines.extend(exec_block)
        else:
            final_lines.append(line)

    # If no exec line was found, append at end
    if exec_line_idx < 0:
        final_lines.extend(exec_block)

    return "\n".join(final_lines) + "\n"


def _is_exec_line(line: str) -> bool:
    """Check if a line looks like the VASP execution command."""
    for pat in _EXEC_PATTERNS:
        if pat.search(line):
            return True
    return False


def _marker_lines(job_name: str) -> list[str]:
    """Generate the exit-code check and marker-touch block."""
    name_safe = job_name  # Slurm job name, used as marker prefix
    return [
        "EXIT_CODE=$?",
        "echo '=== VASP Job Finished at $(date) ==='",
        "echo 'Exit code:' $EXIT_CODE",
        "",
        "if [ $EXIT_CODE -eq 0 ]; then",
        f"    touch {name_safe}_DONE",
        "else",
        f"    touch {name_safe}_FAILED",
        "fi",
    ]


def submit_job(
    job_name: str,
    job_dir: str,
    slurm_cfg: dict,
    vasp_exec: str,
    dependency_jobid: Optional[str] = None,
    dry_run: bool = False,
    pre_exec_cmds: list[str] | None = None,
) -> Optional[str]:
    """Write sbatch script and submit to Slurm.

    Args:
        job_name: SLURM job name.
        job_dir: Working directory for this job step.
        slurm_cfg: Slurm config dict.
        vasp_exec: VASP executable name.
        dependency_jobid: If set, add --dependency=afterok:<JOBID>.
        dry_run: If True, write script but don't submit.
        pre_exec_cmds: Commands to inject before VASP execution.

    Returns:
        Slurm JOBID string if submitted, None if dry_run.
    """
    os.makedirs(job_dir, exist_ok=True)

    script_path = os.path.join(job_dir, f"{job_name}.sbatch")
    script_content = _build_sbatch_script(job_name, job_dir, slurm_cfg, vasp_exec,
                                          pre_exec_cmds=pre_exec_cmds)

    with open(script_path, "w") as f:
        f.write(script_content)

    os.chmod(script_path, 0o755)

    if dry_run:
        logger.info(f"[DRY RUN] sbatch script written: {script_path}")
        return None

    sbatch_bin = os.path.join(SLURM_BIN, "sbatch")
    cmd = [sbatch_bin]
    if dependency_jobid:
        cmd.append(f"--dependency=afterok:{dependency_jobid}")

    cmd.append(script_path)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        raise RuntimeError(f"sbatch failed: {result.stderr.strip()}")

    match = re.search(r"Submitted batch job (\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse sbatch output: {result.stdout}")

    return match.group(1)
