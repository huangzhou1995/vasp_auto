#!/bin/bash
#SBATCH --job-name "vasp"
#SBATCH --partition P100
#SBATCH --comment=""
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --threads-per-core=1
#SBATCH --gpus-per-task=1
#SBATCH --gpu-bind=closest
#SBATCH  --gres-flags=enforce-binding
#SBATCH --gpu-bind=closest
#SBATCH --hint=nomultithread
#SBATCH --export=CUDA_DEVICE_ORDER=PCI_BUS_ID
### Node settings
### CPU  settings
### GPU  setting
ulimit -s unlimited
dos2unix POSCAR
module purge
GPUNAME=$(nvidia-smi --query-gpu=gpu_name --format=csv,noheader -i 0 2>/dev/null)
module load nvhpc_25.3/nvhpc/25.3
module load mkl/2024.2
module  load   vasp/6.4.2-vtst-mkl2024-nvhpc25.3
MPINUM=$SLURM_NTASKS
OMPINUM=${SLURM_CPUS_PER_GPU:-$SLURM_CPUS_PER_TASK}
export OMP_STACKSIZE=4G
export OMP_PLACES=cores
export OMP_PROC_BIND=close
export MKL_THREADING_LAYER=INTEL
export I_MPI_PIN_DOMAIN=numa
export MKL_CBWR=AVX2
export OMP_NUM_THREADS=${OMPINUM}
export HOSTNAME=$(hostname)
export MPIRUN_OPTIONS="--bind-to none --map-by socket:PE=${OMPINUM}"
export MPIRUN_CMD="mpirun -np $MPINUM $MPIRUN_OPTIONS"
export EXECUTABLE=vasp_std
echo "From ${HOSTNAME} ${EXECUTABLE} running on ${GPUNAME}  with ${MPINUM} MPI-tasks and ${OMPINUM} threads"
startexe="$MPIRUN_CMD ${EXECUTABLE}"
echo $startexe
env
exec $startexe