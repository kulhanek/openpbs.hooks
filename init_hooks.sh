#!/bin/bash

for HOOK in hook_discovery_node.qmgr  \
            hook_discovery_cpus.qmgr  \
            hook_discovery_gpus.qmgr  \
            hook_discovery_interconnect.qmgr  \
            hook_job_enqueued.qmgr  \
            hook_job_cgroups_v2.qmgr  \
            hook_job_gpus.qmgr  \
            hook_workspace.qmgr \
            hook_job_env.qmgr; do
    echo ""
    echo "# HOOK: $(basename $HOOK)"
    qmgr < $HOOK
done

