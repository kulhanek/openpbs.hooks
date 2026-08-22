# OpenPBS Hooks for Small Clusters

This repository contains a set of OpenPBS hooks intended for small clusters built from commodity PCs and entry-level servers. The hooks provide automatic node resource discovery, cgroups v2 based CPU and memory isolation, NVIDIA GPU allocation and accounting, job environment setup, and per-job workspace management.

The hooks were tested on **Ubuntu 24.04** and are designed to work with the **cgroups v2** controller. They are primarily aimed at relatively simple OpenPBS installations where execution nodes are managed directly by PBS MoM without an additional cluster resource-management layer.

## Notes
* The code was mostly created in ChatGPT chat (Plus Subscription) and is currently tested on a small cluster. 
* The hooks use some OpenPBS resources with non-standard flags (*hl*, *hu*, *ho*, *ha*), which are implemented in the [modified version of OpenPBS](https://github.com/kulhanek/openpbs).

## Contents
* [Hooks](docs/HOOKS.md)
* [Resources](docs/RESOURCES.md)

