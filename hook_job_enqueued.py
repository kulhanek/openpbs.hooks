# coding: utf-8
"""
OpenPBS queuejob hook: normalize and validate CPU/MPI/OpenMP select chunks.

CPU model
---------
* ncpus is the number of physical CPU cores requested by a chunk.
* smt=true may be requested on its own.  In that case the exact number of
  logical PUs exposed by the later cgroup hook is topology-dependent and this
  hook does not add nthreads.
* npus_per_core is a string vnode property used for an exact match on uniform
  SMT hardware.  When requested, this hook also requires/sets smt=true and
  requires/sets nthreads=ncpus*npus_per_core.
* nthreads is therefore scheduler-consumable only in the explicit uniform-SMT
  path above.  Without npus_per_core, any explicit nthreads must equal ncpus.
* mpiprocs and ompthreads describe the application MPI/OpenMP layout.  They do
  not describe SMT topology.
"""

import re
import traceback

import pbs


HOOK_NAME = "job_enqueued"

_TRUE_VALUES = set(["1", "true", "t", "yes", "y", "on"])
_FALSE_VALUES = set(["0", "false", "f", "no", "n", "off"])
_INTEGER_RE = re.compile(r"^[0-9]+$")
_DEBIAN_RE = re.compile(r"^debian([0-9]+)$")


def log(level, message):
    pbs.logmsg(level, "%s: %s" % (HOOK_NAME, message))


def fail(message):
    raise ValueError(message)


def parse_positive_int(name, value, chunk_no, minimum=1):
    text = str(value).strip()
    if not _INTEGER_RE.match(text):
        fail("chunk %d: %s must be an integer >= %d (got %r)" %
             (chunk_no, name, minimum, value))
    number = int(text)
    if number < minimum:
        fail("chunk %d: %s must be an integer >= %d (got %r)" %
             (chunk_no, name, minimum, value))
    return number


def parse_bool(name, value, chunk_no):
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    fail("chunk %d: %s must be a boolean value (got %r)" %
         (chunk_no, name, value))


class SelectChunk(object):
    """Minimal parser which preserves resource order and chunk multiplicity."""

    def __init__(self, text, chunk_no):
        self.chunk_no = chunk_no
        self.multiplier = None
        self.items = []          # [key, value]
        self.positions = {}

        tokens = str(text).split(":")
        if not tokens or any(token == "" for token in tokens):
            fail("chunk %d: malformed empty select field" % chunk_no)

        start = 0
        if "=" not in tokens[0]:
            self.multiplier = parse_positive_int("chunk multiplier", tokens[0], chunk_no)
            start = 1
            if start >= len(tokens):
                fail("chunk %d: select chunk contains only a multiplier" % chunk_no)

        for token in tokens[start:]:
            if "=" not in token:
                fail("chunk %d: malformed select field %r" % (chunk_no, token))
            key, value = token.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or not value:
                fail("chunk %d: malformed select field %r" % (chunk_no, token))
            if key in self.positions:
                fail("chunk %d: resource %s is specified more than once" %
                     (chunk_no, key))
            self.positions[key] = len(self.items)
            self.items.append([key, value])

    def has(self, key):
        return key in self.positions

    def get(self, key):
        if key not in self.positions:
            return None
        return self.items[self.positions[key]][1]

    def set(self, key, value):
        text = str(value)
        if key in self.positions:
            self.items[self.positions[key]][1] = text
        else:
            self.positions[key] = len(self.items)
            self.items.append([key, text])

    def render(self):
        fields = []
        if self.multiplier is not None:
            fields.append(str(self.multiplier))
        fields.extend("%s=%s" % (key, value) for key, value in self.items)
        return ":".join(fields)


def validate_legacy_os(chunk):
    """Preserve the Debian-version guard from the original hook."""
    os_value = chunk.get("os")
    if os_value is None:
        return
    match = _DEBIAN_RE.match(str(os_value))
    if match and int(match.group(1)) < 10:
        fail("chunk %d: unsupported Debian version %s" %
             (chunk.chunk_no, match.group(1)))


def normalize_chunk(text, chunk_no):
    chunk = SelectChunk(text, chunk_no)
    validate_legacy_os(chunk)

    # ncpus: always materialize it in the normalized chunk.
    if chunk.has("ncpus"):
        ncpus = parse_positive_int("ncpus", chunk.get("ncpus"), chunk_no)
        chunk.set("ncpus", ncpus)
    else:
        ncpus = 1
        chunk.set("ncpus", ncpus)

    # smt is valid independently of npus_per_core (important for hybrid CPUs).
    smt = None
    if chunk.has("smt"):
        smt = parse_bool("smt", chunk.get("smt"), chunk_no)

    # npus_per_core is a string resource for exact vnode matching, but its
    # requested value must be a positive integer and is normalized to decimal.
    npus_per_core = None
    if chunk.has("npus_per_core"):
        npus_per_core = parse_positive_int(
            "npus_per_core", chunk.get("npus_per_core"), chunk_no)
        chunk.set("npus_per_core", npus_per_core)

    # Validate any explicit nthreads before deciding whether to synthesize it.
    explicit_nthreads = None
    if chunk.has("nthreads"):
        explicit_nthreads = parse_positive_int(
            "nthreads", chunk.get("nthreads"), chunk_no)
        chunk.set("nthreads", explicit_nthreads)

    if npus_per_core is not None:
        # Explicit uniform-SMT scheduling path.
        if smt is False:
            fail("chunk %d: npus_per_core requires smt=true" % chunk_no)
        if smt is None:
            chunk.set("smt", "true")
            smt = True

        expected_nthreads = ncpus * npus_per_core
        if explicit_nthreads is None:
            chunk.set("nthreads", expected_nthreads)
            nthreads = expected_nthreads
        else:
            if explicit_nthreads != expected_nthreads:
                fail("chunk %d: nthreads must equal ncpus * npus_per_core "
                     "(%d * %d = %d), got %d" %
                     (chunk_no, ncpus, npus_per_core, expected_nthreads,
                      explicit_nthreads))
            nthreads = explicit_nthreads

        capacity = nthreads
    else:
        # Generic/non-uniform path.  smt=true is allowed, but this hook cannot
        # infer topology, so it does not synthesize an expanded nthreads value.
        # If nthreads is explicitly supplied without npus_per_core, only the
        # physical-core capacity is schedulable/valid here.
        if explicit_nthreads is not None and explicit_nthreads != ncpus:
            fail("chunk %d: nthreads without npus_per_core must equal ncpus "
                 "(%d), got %d" % (chunk_no, ncpus, explicit_nthreads))
        capacity = ncpus

    # MPI process count defaults to one rank per physical core.
    if chunk.has("mpiprocs"):
        mpiprocs = parse_positive_int(
            "mpiprocs", chunk.get("mpiprocs"), chunk_no)
        chunk.set("mpiprocs", mpiprocs)
    else:
        mpiprocs = ncpus
        chunk.set("mpiprocs", mpiprocs)

    if mpiprocs > ncpus:
        fail("chunk %d: mpiprocs must satisfy 1 <= mpiprocs <= ncpus "
             "(%d), got %d" % (chunk_no, ncpus, mpiprocs))

    # OpenMP thread count is application-level and deliberately independent
    # of SMT.  Default to one OpenMP thread per MPI process.
    if chunk.has("ompthreads"):
        ompthreads = parse_positive_int(
            "ompthreads", chunk.get("ompthreads"), chunk_no)
        chunk.set("ompthreads", ompthreads)
    else:
        ompthreads = 1
        chunk.set("ompthreads", ompthreads)

    application_threads = mpiprocs * ompthreads
    if application_threads > capacity:
        if npus_per_core is not None:
            fail("chunk %d: mpiprocs * ompthreads (%d * %d = %d) exceeds "
                 "nthreads=%d" %
                 (chunk_no, mpiprocs, ompthreads, application_threads, capacity))
        fail("chunk %d: mpiprocs * ompthreads (%d * %d = %d) exceeds "
             "ncpus=%d; request npus_per_core to use the explicit uniform-SMT "
             "scheduling path" %
             (chunk_no, mpiprocs, ompthreads, application_threads, capacity))

    return chunk.render()


def explicit_smt_value(chunk_text, chunk_no):
    """Return explicit SMT value from a normalized chunk, or None if omitted."""
    chunk = SelectChunk(chunk_text, chunk_no)
    if not chunk.has("smt"):
        return None
    return parse_bool("smt", chunk.get("smt"), chunk_no)


def normalize_select(select_text):
    text = str(select_text).strip()
    if not text:
        fail("select request is empty")

    normalized = [normalize_chunk(chunk, index + 1)
                  for index, chunk in enumerate(text.split("+"))]

    # SMT is implemented by the execution hook as a job-wide cpuset policy.
    # Therefore all chunks which explicitly specify smt must agree.  Chunks
    # which omit smt inherit the resulting job-wide setting.
    explicit_values = set()
    for index, chunk in enumerate(normalized):
        value = explicit_smt_value(chunk, index + 1)
        if value is not None:
            explicit_values.add(value)

    if len(explicit_values) > 1:
        fail("contradictory smt requests across select chunks")

    return "+".join(normalized)


def main():
    e = pbs.event()
    if e.type != pbs.QUEUEJOB:
        e.accept()
        return

    job = e.job

    if "nodes" in job.Resource_List.keys():
        e.reject("Old syntax rejected. Please use 'select' syntax.")
        return

    if "select" not in job.Resource_List.keys():
        # No explicit select expression to normalize.
        e.accept()
        return

    old_select = str(job.Resource_List["select"])
    new_select = normalize_select(old_select)

    log(pbs.EVENT_DEBUG, "old select: %s" % old_select)
    log(pbs.EVENT_DEBUG, "new select: %s" % new_select)
    job.Resource_List["select"] = pbs.select(new_select)
    e.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    try:
        log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
        pbs.event().reject("%s hook failed: %s" % (HOOK_NAME, exc))
    except Exception:
        pass
