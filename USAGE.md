# vasp_auto — VASP 自动化计算工具

自动通过 Slurm 提交 VASP 结构优化 → 自洽 SCF → 非自洽 DOS，自动检测作业完成并推进。
支持断点续算、自动生成 POTCAR/KPOINTS。

## 前置要求

- Python 3.8+, PyYAML (`pip install pyyaml`)
- Slurm 作业系统 (sbatch / squeue / sacct)
- VASP (vasp_std / vasp_gam / vasp_ncl) 或 qvasp
- vaspkit (用于 POTCAR 生成和后处理，需配置 VASP_POT_PATH)

## 快速开始

```bash
# 1. 进入算例目录，准备 POSCAR (POTCAR, KPOINTS 可自动生成)
cd /path/to/your/calculation
ls POSCAR                      # 只需要 POSCAR

# 2. 如果你已有 sbatch 脚本模板，直接指向它:
#    (在 config.yaml 中设置 slurm.template: "./my_sbatch.sh")
#    程序自动替换 job-name/output/error，注入 cd 和完成检测

# 3. 复制配置文件并编辑
cp /path/to/vasp_auto/config.yaml .
vim config.yaml                # 改 INCAR 参数（如果用模板则 Slurm 项可忽略）

# 4. 运行完整流程 (自动生成 POTCAR → KPOINTS → opt → scf → dos)
python /path/to/vasp_auto/vasp_auto.py config.yaml
```

## 配置文件说明

```yaml
slurm:
  partition: "compute"         # Slurm 分区名
  nodes: 1
  ntasks_per_node: 32          # 每节点核数
  time: "24:00:00"             # 最大运行时间
  job_name_prefix: "vasp"      # 作业名前缀
  extra: ""                    # 额外 #SBATCH 指令

vasp:
  exec: "vasp_std"             # vasp_std | vasp_gam | vasp_ncl | qvasp

work_dir: "./"                 # 算例目录

# 运行模式
#   sequential - 跑完一步等待完成，再提交下一步 (推荐)
#   chain      - 用 Slurm dependency 一次性提交三步
mode: "sequential"

poll_interval: 60              # 轮询间隔(秒)

# ---- 输入文件自动生成 ----
potcar:
  auto_generate: true          # 用 vaspkit 103 从 POSCAR 自动生成 POTCAR

kpoints:
  auto_generate: true          # 自动生成 KPOINTS
  mode: "direct"               # "direct"=程序直接写入 | "vaspkit"=vaspkit 102
  scheme: "M"                  # G=Gamma-centered | M=Monkhorst-Pack
  mesh: [0, 0, 0]             # [kx, ky, kz], [0,0,0]=根据密度自动计算
  kpoints_density: 0.04        # K 点密度 (/埃^-1)

# ---- 断点续算 ----
resume:
  skip_completed: true         # 跳过已有 _DONE 标记的步骤
  retry_failed: true           # 失败步骤自动重试
  max_retries: 1               # 最大重试次数

steps:
  opt:                         # 结构优化
    enabled: true
    incar:
      ISIF: 3                  # 全弛豫
      IBRION: 2                # 共轭梯度
      NSW: 100
      EDIFFG: -0.02            # 力收敛标准 (eV/A)

  scf:                         # 自洽静态计算
    enabled: true
    incar:
      NSW: 0
      ICHARG: 2

  dos:                         # 非自洽 DOS 计算
    enabled: true
    incar:
      NSW: 0
      ICHARG: 11
      ISMEAR: -5               # 四面体方法
      LORBIT: 11

postprocess:
  vaspkit_tasks: []            # 后处理任务号: 301=DOS提取, 211=能带
```

## 使用已有 sbatch 脚本 (推荐)

如果你已经有适配集群的 sbatch 脚本（module load、GPU 绑定等），直接作为模板使用，无需在 config.yaml 中重复配置 Slurm 参数：

```yaml
slurm:
  template: "./sub_vasp_gpu.sh"  # 指向你已有的 sbatch 脚本
```

程序会：
1. 保留所有 #SBATCH 指令、module load、环境变量
2. 自动替换 `--job-name` 为步骤名 (opt/scf/dos)
3. 自动替换 `--output` / `--error` 路径
4. 找到 `mpirun/srun/exec ... vasp_std` 那一行
5. 在前面注入 `cd <工作目录>`
6. 在后面注入完成检测逻辑（exit code 检查 + `_DONE`/`_FAILED` 标记）
7. 如果执行行用了 `exec`，自动去掉（否则后面的标记逻辑不会执行）

**模板示例 (CPU 集群):**
```bash
#!/bin/bash
#SBATCH --job-name=vasp-test
#SBATCH --partition=P_96
#SBATCH --nodes=1
#SBATCH --ntasks=96
#SBATCH --output=%j.log

module load intel/oneapi2025.2
module load vasp/6.3.2-vtst-sol-cp-plugins-oneapi2025

mpirun -np $SLURM_NTASKS vasp_std
```

**模板示例 (GPU 集群):**
```bash
#!/bin/bash
#SBATCH --job-name "vasp"
#SBATCH --partition P100
#SBATCH --gpus-per-task=1
...
module load nvhpc/25.3
...
mpirun -np $MPINUM $MPIRUN_OPTIONS vasp_std
```

## 命令行用法

```bash
# 完整工作流
python vasp_auto.py config.yaml

# 只运行某一步 (opt / scf / dos)
python vasp_auto.py --step scf config.yaml

# 检查某个作业的状态
python vasp_auto.py --check 12345

# 检查作业状态 + 扫描 OUTCAR 错误
python vasp_auto.py --check 12345 config.yaml

# 只看 sbatch 脚本，不实际提交
python vasp_auto.py --dry-run config.yaml

# 仅运行后处理 (vaspkit)
python vasp_auto.py --post config.yaml

# 调试模式 (详细日志)
python vasp_auto.py --verbose config.yaml
```

## 工作流程

```
输入: POSCAR (只需这一个文件!)
  │
  ├─[自动生成] POTCAR (vaspkit 103)
  ├─[自动生成] KPOINTS (根据密度从晶格自动算 mesh)
  │
  ├─[Step 1] opt/
  │    生成 INCAR (ISIF=3, IBRION=2)
  │    sbatch 提交 → 轮询等待 → 检测完成/错误
  │    成功: opt_DONE  失败: opt_FAILED
  │    输出: CONTCAR
  │
  ├─[Step 2] scf/
  │    复制 CONTCAR → POSCAR
  │    生成 INCAR (NSW=0, ICHARG=2)
  │    sbatch 提交 → 轮询等待 → 检测完成/错误
  │    成功: scf_DONE  失败: scf_FAILED
  │    输出: CHGCAR
  │
  ├─[Step 3] dos/
  │    复制 CHGCAR
  │    生成 INCAR (NSW=0, ICHARG=11)
  │    sbatch 提交 → 轮询等待 → 检测完成/错误
  │    成功: dos_DONE  失败: dos_FAILED
  │
  └─[后处理] vaspkit (提取 DOS / 能带)
```

## 断点续算

每个步骤完成后会生成 `{step}_DONE` 标记文件。重新运行时：

- **已完成步骤**: 自动跳过 (skip_completed=true)
- **失败步骤**: 自动重试 (retry_failed=true, 最多 max_retries 次)
- **未运行步骤**: 正常启动

```bash
# 场景：opt 已完成，scf 在排队时断电了
python vasp_auto.py config.yaml
# 输出: Step 'opt' already completed — skipping
#       提交 scf ...
```

## KPOINTS 自动生成

支持两种模式：

1. **direct** (推荐): 程序直接从 POSCAR 晶格向量计算倒空间，按密度自动算 mesh
   ```yaml
   kpoints:
     mode: "direct"
     scheme: "G"
     mesh: [0, 0, 0]           # 0=自动计算
     kpoints_density: 0.04
   ```

2. **vaspkit**: 调用 vaspkit task 102 交互生成
   ```yaml
   kpoints:
     mode: "vaspkit"
     scheme: "G"
     kpoints_density: 0.04
   ```

也可以手动指定 mesh：`mesh: [6, 6, 1]`

## 自动检测机制

**完成检测**: OUTCAR 末尾出现 `General timing and accounting`

**错误检测**: 自动扫描 OUTCAR 中以下关键词:
- `SIGSEGV` — 段错误
- `ZPOTRF` — 电子步对角化失败
- `EDDDAV` — 电子步最小化错误
- `EEEEE` — 数值溢出
- `WARNING: electronic step limit` — SCF 不收敛

## 两种模式对比

|        | sequential (推荐) | chain |
|--------|-------------------|-------|
| 提交方式 | 一步一步提交 | 一次提交三步 |
| 耦合方式 | Python 轮询等待 | sbatch --dependency |
| 失败处理 | 检测到错误立即停止/重试 | Slurm 自动跳过失败依赖 |
| 断点续算 | 支持 | 需手动管理 |
| 监控 | 实时日志输出 | 需手动 squeue 查 |
| 适用场景 | 交互使用、单机 | 批量自动化 |

## 常见问题

**Q: 只有 POSCAR，没有 POTCAR 和 KPOINTS？**
程序会自动生成它们：POTCAR 用 vaspkit 103，KPOINTS 用程序直接写入。确保 vaspkit 已配置 VASP_POT_PATH。

**Q: 作业一直 PENDING 不跑？**
检查 Slurm 分区和资源配置：`sinfo` 看分区状态，`squeue -j JOBID` 看具体原因。

**Q: opt 步失败重试后自动用最新的 CONTCAR 吗？**
是的。scf 步会检查 CONTCAR 修改时间，如果比已有 POSCAR 新则自动复制。

**Q: 如何跳过某一步？**
config.yaml 中设 `enabled: false`

**Q: 如何指定不同步骤用不同 KPOINTS？**
把不同 KPOINTS 放到对应步骤目录 (opt/KPOINTS, dos/KPOINTS)，程序检测到已有文件会跳过生成。

**Q: 想从头重算，如何清除断点状态？**
```bash
rm -rf opt/ scf/ dos/
# 或只删标记文件
rm opt/opt_DONE scf/scf_DONE dos/dos_DONE
```
