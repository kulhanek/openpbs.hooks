# `hook_discovery_gpus`

## 1. Overview

`hook_discovery_gpus` discovers physical NVIDIA GPUs with `nvidia-smi` and publishes GPU capacity and descriptive properties on the local PBS vnode. It is intended for `exechost_startup` and `exechost_periodic`.

The hook has no DCGM or AMS dependency. It counts **physical NVIDIA GPUs only**; NVIDIA MIG instances are deliberately not treated as independently schedulable GPUs.

## 2. User documentation

### Published resources

| Resource | Meaning | Example use |
|---|---|---|
| `ngpus` | Number of physical NVIDIA GPUs visible to `nvidia-smi`. | `select=1:ncpus=8:ngpus=1` |
| `gpu_model` | Unique NVIDIA GPU model name(s) on the node. | Select nodes by model if the resource is configured for scheduling. |
| `gpu_cap` | Unique CUDA compute capability value(s), when supported by the installed driver/`nvidia-smi`. | Select nodes requiring a particular capability value. |
| `cuda_version` | Maximum CUDA version reported by the installed NVIDIA driver in normal `nvidia-smi` output. | Select nodes compatible with a required driver CUDA level, subject to site resource semantics. |

Suggested custom PBS resources from the hook source are:

```text
gpu_model    : string_array
gpu_cap      : string_array
cuda_version : string
```

`ngpus` is the standard GPU count resource in PBS installations configured for GPU scheduling.

### Example requests

One GPU with eight CPU cores:

```bash
#PBS -l select=1:ncpus=8:ngpus=1
```

Two GPUs:

```bash
#PBS -l select=1:ncpus=16:ngpus=2
```

A model-specific request can be used if `gpu_model` is defined and made schedulable according to site policy, for example:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_model=<configured-model-value>
```

### Restrictions

- Only NVIDIA GPUs discoverable through the configured `nvidia-smi` binary are supported.
- MIG instances are ignored as scheduling units; `ngpus` is a physical-GPU count.
- This hook only discovers/publishes GPU resources. Per-job allocation, isolation, environment setup, and accounting are handled by `hook_job_gpus`.
- If the `nvidia-smi` executable is absent, the hook publishes `ngpus=0` and clears the descriptive GPU properties.
- On older drivers where the `compute_cap` query field is unsupported, model/count discovery continues but `gpu_cap` can be empty.

## 3. Technical documentation

### Operation

The hook executes:

```text
nvidia-smi --query-gpu=index,name,compute_cap --format=csv,noheader,nounits
```

If that query fails, it falls back to:

```text
nvidia-smi --query-gpu=index,name --format=csv,noheader,nounits
```

The number of successfully parsed rows becomes `ngpus`. Model names and compute capabilities are de-duplicated and sorted into comma-separated values suitable for `string_array` resources.

The hook separately runs ordinary `nvidia-smi` and extracts the `CUDA Version:` field to produce `cuda_version`. It then publishes the resulting values only to local vnodes. Empty model/capability/CUDA values are assigned as `None`, which also clears stale vnode values if a property disappears.

### JSON configuration

| Item | Type | Default | Description |
|---|---:|---:|---|
| `nvidia_smi` | string/path | `"/usr/bin/nvidia-smi"` | Absolute path to the NVIDIA `nvidia-smi` executable used for discovery. |

### Limitations and failure behaviour

- The implementation assumes CSV output fields do not contain unexpected commas beyond the parsing strategy used by the hook.
- CUDA compatibility is represented by the driver-reported `CUDA Version`, not by detecting an installed CUDA Toolkit.
- If the executable exists but both GPU queries fail, the hook raises an error and rejects the event.
- Local-vnode identification is based on PBS/local host names; absence of a matching local vnode causes event rejection.
- The hook does not create custom PBS resource definitions or scheduler configuration; those must be installed separately.
