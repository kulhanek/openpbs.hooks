import pbs
import re

try:
    e = pbs.event()

    if e.type == pbs.EXECJOB_BEGIN:
        j = e.job
        node=pbs.get_local_nodename()
        pbs.logmsg(pbs.EVENT_DEBUG, "env hook, node: %s" % node)
        pbs.logmsg(pbs.EVENT_DEBUG, "env hook, %s has exec_vnode: %s" % (j.id, str(j.exec_vnode)))
        
        resources = {}
        for i in str(j.exec_vnode).split("+"):
            i = i.replace("(","")
            i = i.replace(")","")

            node_i = i.split(":")[0].split(".")[0]

            if not node_i in resources.keys():
                resources[node_i] = {"ncpus":0, "mem":0, "ngpus":0}

            m = re.search('ncpus=([0-9]+)', i)
            if m:
                resources[node_i]["ncpus"] += int(m.group(1))

            m = re.search('ngpus=([0-9]+)', i)
            if m:
                resources[node_i]["ngpus"] += int(m.group(1))

            m = re.search('mem=([0-9]+)kb', i)
            if m:
                resources[node_i]["mem"] += int(m.group(1)) * 1024

        pbs.logmsg(pbs.EVENT_DEBUG, "env hook, resources: %s" % str(resources))

        if node in resources.keys():
            j.Variable_List["PBS_RESC_MEM"] = resources[node]["mem"]
            j.Variable_List["TORQUE_RESC_MEM"] = resources[node]["mem"]

            j.Variable_List["PBS_NUM_PPN"] = resources[node]["ncpus"]
            j.Variable_List["PBS_NCPUS"] = resources[node]["ncpus"]
            j.Variable_List["TORQUE_RESC_PROC"] = resources[node]["ncpus"]
            j.Variable_List["PBS_NGPUS"] = resources[node]["ngpus"]

            # Prefer values recorded by the cgroup hook in resources_used.
            # pbs_resource objects do not reliably implement dict-style iteration,
            # therefore access custom resources as attributes.
            nthreads = None
            hyperthreading = None

            try:
                nthreads = getattr(j.resources_used, "nthreads", None)
            except Exception:
                nthreads = None

            try:
                hyperthreading = getattr(j.resources_used, "hyperthreading", None)
            except Exception:
                hyperthreading = None

            if nthreads is None:
                # Safe fallback: without an explicit value from the cgroup hook,
                # the number of usable threads is at least the PBS ncpus allocation.
                nthreads = resources[node]["ncpus"]
                pbs.logmsg(pbs.EVENT_DEBUG,
                           "env hook, resources_used.nthreads unavailable; "
                           "falling back to local ncpus=%s" % str(nthreads))
            else:
                nthreads = int(nthreads)

            if hyperthreading is None:
                # Safe fallback corresponding to nthreads == ncpus.
                hyperthreading_enabled = False
                pbs.logmsg(pbs.EVENT_DEBUG,
                           "env hook, resources_used.hyperthreading unavailable; "
                           "falling back to n")
            else:
                hyperthreading_enabled = str(hyperthreading).strip().lower() in (
                    "1", "true", "t", "yes", "y", "on"
                )

            j.Variable_List["PBS_NTHREADS"] = str(nthreads)
            j.Variable_List["PBS_HYPERTHREADING"] = (
                "y" if hyperthreading_enabled else "n"
            )

            pbs.logmsg(pbs.EVENT_DEBUG,
                       "env hook, PBS_NTHREADS=%s PBS_HYPERTHREADING=%s" %
                       (j.Variable_List["PBS_NTHREADS"],
                        j.Variable_List["PBS_HYPERTHREADING"]))

        total_mem = 0
        for node_i in resources.keys():
            total_mem += resources[node_i]["mem"]
        j.Variable_List["PBS_RESC_TOTAL_MEM"] = total_mem
        j.Variable_List["TORQUE_RESC_TOTAL_MEM"] = total_mem
        
        total_ncpus = 0
        for node_i in resources.keys():
            total_ncpus += resources[node_i]["ncpus"]
        j.Variable_List["PBS_RESC_TOTAL_PROCS"] = total_ncpus
        j.Variable_List["TORQUE_RESC_TOTAL_PROCS"] = total_ncpus        

        j.Variable_List["PBS_NUM_NODES"] = len(resources.keys())

        if "walltime" in j.Resource_List.keys():
            walltime = int(j.Resource_List["walltime"])
            j.Variable_List["PBS_RESC_TOTAL_WALLTIME"] = walltime
            j.Variable_List["TORQUE_RESC_TOTAL_WALLTIME"] = walltime

        pbs.logmsg(pbs.EVENT_DEBUG, "env hook, new Variable_List: %s" % str(j.Variable_List))
        
except SystemExit:
    pass
except:
    e.reject("env hook failed")
