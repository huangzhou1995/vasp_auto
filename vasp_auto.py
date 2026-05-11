#!/usr/bin/env python3
"""vasp_auto — Automated VASP workflow via Slurm.

Usage:
    python vasp_auto.py config.yaml          # Run full workflow
    python vasp_auto.py --step opt config.yaml  # Run single step
    python vasp_auto.py --check JOBID        # Check job status
    python vasp_auto.py --post config.yaml   # Post-process only
    python vasp_auto.py --dry-run config.yaml # Generate scripts only
"""

import argparse
import logging
import sys
import os

# Ensure core package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config, validate_config
from core.workflow import run_workflow
from core.monitor import check_job_status, check_outcar_errors
from core.postprocess import run_postprocessing


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description="vasp_auto — Automate VASP structure optimization, SCF, and DOS calculations via Slurm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vasp_auto.py config.yaml              # Full workflow
  python vasp_auto.py --step opt config.yaml   # Only structure optimization
  python vasp_auto.py --step dos config.yaml   # Only DOS step
  python vasp_auto.py --check 12345            # Check job 12345 status
  python vasp_auto.py --check 12345 config.yaml # Check + scan OUTCAR
  python vasp_auto.py --post config.yaml       # Run vaspkit post-processing
  python vasp_auto.py --dry-run config.yaml    # Generate sbatch scripts only
        """,
    )

    parser.add_argument("config", nargs="?", help="Path to config YAML file")
    parser.add_argument("--step", choices=["opt", "scf", "dos"], help="Run only a single step")
    parser.add_argument("--check", metavar="JOBID", help="Check status of a Slurm job")
    parser.add_argument("--post", action="store_true", help="Run post-processing only")
    parser.add_argument("--dry-run", action="store_true", help="Generate sbatch scripts without submitting")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # --check mode
    if args.check:
        jobid = args.check
        status = check_job_status(jobid)
        print(f"Job {jobid}:")
        print(f"  State:   {status['state']}")
        print(f"  Message: {status['message']}")

        if args.config:
            cfg = load_config(args.config)
            work_dir = cfg["work_dir"]
            # Try to find OUTCAR
            for step_name in ["opt", "scf", "dos"]:
                step_dir = os.path.join(
                    work_dir, cfg["steps"][step_name].get("dir", step_name)
                )
                outcar = os.path.join(step_dir, "OUTCAR")
                if os.path.isfile(outcar):
                    errors = check_outcar_errors(outcar)
                    if errors:
                        print(f"\n  OUTCAR warnings in {step_dir}:")
                        for e in errors:
                            print(f"    - {e}")
                    else:
                        print(f"\n  OUTCAR ({step_dir}): no errors detected")
        return

    # Require config file for other modes
    if not args.config:
        parser.error("config file required (unless using --check)")

    cfg = load_config(args.config)

    # Validate
    issues = validate_config(cfg)
    if issues:
        logger.warning("Configuration warnings:")
        for issue in issues:
            logger.warning(f"  - {issue}")

    # --post mode
    if args.post:
        run_postprocessing(cfg)
        return

    # Workflow mode (full or single step)
    dry_run = args.dry_run
    step = args.step

    if dry_run:
        logger.info("DRY RUN mode — sbatch scripts will be generated but not submitted")

    try:
        run_workflow(cfg, step_filter=step, dry_run=dry_run)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
