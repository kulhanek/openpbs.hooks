# `hook_normalize_job_gpucap`

## 1. Overview

`hook_normalize_job_gpucap` is a server-side OpenPBS normalization hook for
`queuejob` and `modifyjob`.

It rewrites only `gpu_cap` values inside `Resource_List.select`. Its purpose is
to convert convenient user syntax into the ordinary comma-separated
`string_array` value consumed by `pbs_sched`.

The hook does **not** decide whether a vnode satisfies the request. Final
matching remains a scheduler responsibility.

The intended resource definition is:

```qmgr
create resource gpu_cap
set resource gpu_cap type = string_array
set resource gpu_cap flag = ho
```

At this site, `ho` gives OR/ANY matching for `string_array`: the first matching
capability is sufficient.

The hook shares its JSON configuration with `hook_discovery_gpus`.

---

## 2. Select backup

Before this hook changes `Resource_List.select`, it checks:

```text
Resource_List.original_select
```

If the original_select is `None` or empty, the current select is copied there. If a
previous normalization hook has already populated it, the value is preserved.

This permits several normalization hooks to form a pipeline while retaining the
select expression seen by the first hook that actually modifies it.

The `original_select` resource is deliberately **not** defined by the supplied
qmgr file; it is assumed to be owned by the common normalization setup.

---

## 3. User syntax

Each comma-separated `gpu_cap` token is processed independently.

### Plain token

```text
gpu_cap=sm_86
```

With:

```json
"use_compatible_gpu_cap": false
```

the token remains:

```text
gpu_cap=sm_86
```

With:

```json
"use_compatible_gpu_cap": true
```

the token is extended by later configured capabilities with the same architecture.

### Exact token

```text
gpu_cap=exact[sm_86]
```

normalizes to:

```text
gpu_cap=sm_86
```

and never requests compatibility expansion.

The syntax is generic. For a future AMD capability, for example:

```text
gpu_cap=exact[gfx942]
```

normalizes to:

```text
gpu_cap=gfx942
```

### Compatibility token

```text
gpu_cap=compat[sm_86]
```

always requests compatibility expansion, independently of
`use_compatible_gpu_cap`.

Again, the syntax is generic:

```text
gpu_cap=compat[gfx942]
```

is valid. If `gfx942` is not found in any configured architecture map, it is
simply retained as `gfx942`; the hook does not reject it.

### NVIDIA `compute_XX`

The NVIDIA convenience notation:

```text
compute_XX
```

is canonicalized to:

```text
sm_XX
```

before compatibility lookup.

The canonical value is still `sm_XX`, but compatibility expansion preserves
the semantic distinction between `compute_XX` and `sm_XX`.

For example:

```text
exact[compute_86]  -> sm_86
compat[compute_86] -> sm_86 and every later configured SM capability
```

Thus `compat[compute_86]` treats `sm_86` as a minimum compute capability and
crosses architecture boundaries.

By contrast:

```text
compat[sm_86]
```

adds only later capabilities that have the same configured architecture as
`sm_86`.

The same distinction applies to plain `compute_XX` and `sm_XX` when
`use_compatible_gpu_cap` is `true`.

This alias is NVIDIA-specific; the `exact[]` and `compat[]` mechanisms
themselves remain vendor-neutral.

---

### Compatibility expansion summary

| User form | Canonical value | Compatibility expansion |
|---|---|---|
| `exact[sm_XX]` | `sm_XX` | none |
| `compat[sm_XX]` | `sm_XX` | later entries with the same architecture |
| `exact[compute_XX]` | `sm_XX` | none |
| `compat[compute_XX]` | `sm_XX` | all later entries, regardless of architecture |
| plain `sm_XX` | `sm_XX` | same as `compat[sm_XX]` only when `use_compatible_gpu_cap=true` |
| plain `compute_XX` | `sm_XX` | same as `compat[compute_XX]` only when `use_compatible_gpu_cap=true` |

## 4. Wrapper validation

`exact[]` and `compat[]` deliberately perform only structural validation.

The value inside brackets:

- must be non-empty;
- must not contain a comma.

Capability naming itself is not validated by the hook.

Thus future capability namespaces can be introduced through configuration
without changing Python code.

Examples rejected as malformed:

```text
exact[]
compat[]
compat[a,b]
exact[foo
```

Examples accepted even when they are not present in the current configuration:

```text
exact[future_cap]
compat[future_cap]
```

An unknown `compat[...]` token is kept unchanged after removal of the wrapper.

---

## 5. Compatibility groups

Compatibility is derived from:

```text
vendors.<vendor>.architectures
```

For example:

```json
"architectures": {
    "sm_80": "ampere",
    "sm_86": "ampere",
    "sm_87": "ampere",
    "sm_89": "ada"
}
```

defines an ordered compatibility sequence.

The order in the JSON mapping is significant and is assumed to run from oldest
to newest. Compatibility expansion is **forward-only** within the same
architecture.

For `sm_XX`, expansion is forward-only and architecture-constrained:

```text
compat[sm_80] -> sm_80,sm_86,sm_87
compat[sm_86] -> sm_86,sm_87
compat[sm_87] -> sm_87
```

For `compute_XX`, the canonical `sm_XX` entry is treated as a minimum and all
later configured capabilities in the same vendor map are eligible, regardless
of architecture. With the supplied NVIDIA ordering, for example:

```text
compat[compute_86]
    -> sm_86,sm_87,sm_89,sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

Optional `state_file` filtering may subsequently remove only the alternatives
added by the hook. The canonical user value (`sm_86` above) is always retained.

If the requested capability is not found in the configuration, it is kept as
is and no compatibility values are added.

Compatibility groups are vendor-local. The hook never combines capabilities
merely because two different vendors use the same architecture label.

The insertion order of `vendors.<vendor>.architectures` is part of the
configuration semantics. Entries must be ordered globally from oldest to newest
for that vendor. `sm_XX` compatibility uses later entries with the same
architecture; `compute_XX` compatibility uses every later entry regardless of
architecture.

If the same capability key is present in more than one vendor map, the lookup
is ambiguous. The hook logs a warning and keeps the user value without adding
compatibility alternatives.

The vendor `enabled` setting controls hardware discovery. It does not remove
the vendor's syntax/mapping from the normalizer.

---

## 6. Optional cluster-inventory filtering

The shared configuration may contain:

```json
"state_file": "server_priv/hooks/hook_data/aggregate_resources.json"
```

A relative path is resolved below `PBS_HOME`.

If `state_file` is absent from the JSON configuration, filtering is disabled.

If it is configured but the file does not exist, cannot be read, or contains
invalid/unusable data, the hook logs the condition and skips filtering. The
state file is an optimization, not a correctness requirement.

The expected data is:

```json
{
    "resources": {
        "gpu_cap": [
            "sm_61",
            "sm_80",
            "sm_89"
        ]
    }
}
```

The aggregation hook is expected to publish this file atomically.

### Only hook-added values are filtered

Values supplied by the user are never removed by inventory filtering.

For example:

```text
input:
    gpu_cap=compat[sm_86]

configured Ampere sequence:
    sm_80,sm_86,sm_87

cluster inventory:
    sm_80,sm_87,sm_89
```

is processed as:

```text
user value:
    sm_86

hook-added candidates:
    sm_87

inventory-filtered additions:
    sm_87
```

and the final request becomes:

```text
gpu_cap=sm_86,sm_87
```

The user-provided capability therefore remains represented even when it is not
present in the current inventory.

---

## 7. Sorting and compaction

After canonicalization, expansion, and optional inventory filtering, the hook
forms one unique sorted list.

For example, an intermediate set such as:

```text
sm_86,sm_80,sm_86,sm_87
```

becomes:

```text
sm_80,sm_86,sm_87
```

Capability names are sorted as ordinary strings. The final stage does not
interpret NVIDIA `sm_*`, AMD `gfx*`, or any future namespace numerically.

This is appropriate because `gpu_cap` uses OR/ANY semantics and list order has
no scheduling meaning.

It also makes normalized selects deterministic and naturally resistant to
growth during repeated normalization.

---

## 8. Multi-chunk select handling

Every select chunk is processed independently.

Example:

```text
2:ncpus=8:ngpus=1:gpu_cap=compat[sm_86]+1:ncpus=16+1:ncpus=4:ngpus=1:gpu_cap=exact[compute_89]
```

Assuming all Ampere alternatives survive filtering, the normalized select is:

```text
2:ncpus=8:ngpus=1:gpu_cap=sm_80,sm_86,sm_87+1:ncpus=16+1:ncpus=4:ngpus=1:gpu_cap=sm_89
```

Chunks without `gpu_cap` are unchanged.

Other resources and the chunk multiplicity are preserved textually.

---

## 9. Shared JSON configuration

The supplied shared configuration is:

```json
{
    "use_compatible_gpu_cap": false,
    "state_file": "server_priv/hooks/hook_data/aggregate_resources.json",
    "vendors": {
        "nvidia": {
            "enabled": true,
            "commands": {
                "nvidia_smi": "/usr/bin/nvidia-smi"
            },
            "architectures": {
                "sm_50": "maxwell",
                "sm_52": "maxwell",
                "sm_53": "maxwell",
                "sm_60": "pascal",
                "sm_61": "pascal",
                "sm_62": "pascal",
                "sm_70": "volta",
                "sm_72": "volta",
                "sm_75": "turing",
                "sm_80": "ampere",
                "sm_86": "ampere",
                "sm_87": "ampere",
                "sm_89": "ada",
                "sm_90": "hopper",
                "sm_100": "blackwell",
                "sm_103": "blackwell",
                "sm_110": "blackwell",
                "sm_120": "blackwell",
                "sm_121": "blackwell"
            }
        }
    }
}
```

The same source file is imported as the configuration for both
`discovery_gpus` and `normalize_job_gpucap`. `hook_discovery_gpus` ignores the
normalizer-specific top-level settings.

### Configuration items

| Item | Default | Meaning |
|---|---:|---|
| `use_compatible_gpu_cap` | `false` | Expand plain capability tokens using their configured compatibility group. |
| `state_file` | absent in Python defaults | If present, filter only hook-added compatibility values against aggregated cluster `gpu_cap` inventory. |
| `vendors.<vendor>.architectures` | `{}` | Vendor-local capability-to-architecture map used to construct compatibility groups. |
| `vendors.<vendor>.enabled` | vendor-specific | Discovery policy; retained in the shared configuration. |
| `vendors.<vendor>.commands` | vendor-specific | Discovery command paths; ignored by this normalizer. |

Although Python defaults do not enable state filtering, the supplied site JSON
does contain `state_file`, so filtering is enabled in the supplied
configuration.

---

## 10. qmgr setup

The supplied qmgr setup creates:

```text
hook name : normalize_job_gpucap
type      : site
events    : queuejob, modifyjob
enabled   : true
user      : pbsadmin
alarm     : 30
order     : 20
debug     : false
fail_action: none
```

No resource is created by this file.

Prerequisites are:

```qmgr
gpu_cap       type=string_array flag=ho
select_backup defined by the common normalization setup
```

The hook imports:

```text
hook_normalize_job_gpucap.py
```

and uses the same source JSON as GPU discovery:

```text
hook_discovery_gpus.json
```

If another queue-time normalization hook must run before or after this one,
adjust `order` consistently across the normalization pipeline.

---

## 11. Failure behavior

The hook rejects a submission for malformed explicit wrapper syntax, for
example an empty `compat[]`.

Unknown capability names are **not** errors.

A missing or invalid optional `state_file` does **not** reject the job.

An unexpected internal error is logged with a traceback and the event is
rejected, because accepting a partially normalized select would make the
pipeline state ambiguous.

---

## 12. Parser scope

The implementation intentionally uses the textual select representation:

```text
chunk+chunk+...
```

and:

```text
count:resource=value:resource=value
```

It assumes resource values used by this site's select syntax do not contain
literal `:` or `+` separators.

For `gpu_cap`, commas delimit OR/ANY capability tokens. Commas are forbidden
inside `exact[...]` and `compat[...]`.

The hook does not synthesize a `select` if the job has none; such jobs are
accepted unchanged.
